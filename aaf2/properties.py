
from __future__ import (
    unicode_literals,
    absolute_import,
    print_function,
    division,
    )

import uuid
from StringIO import StringIO
from .utils import (
    read_u8,
    read_u16le,
    read_u32le,
    write_u8,
    write_u16le,
    write_u32le,
    mangle_name,
    )

SF_DATA                                   = 0x82
SF_DATA_STREAM                            = 0x42
SF_STRONG_OBJECT_REFERENCE                = 0x22
SF_STRONG_OBJECT_REFERENCE_VECTOR         = 0x32
SF_STRONG_OBJECT_REFERENCE_SET            = 0x3A
SF_WEAK_OBJECT_REFERENCE                  = 0x02
SF_WEAK_OBJECT_REFERENCE_VECTOR           = 0x12
SF_WEAK_OBJECT_REFERENCE_SET              = 0x1A
SF_WEAK_OBJECT_REFERENCE_STORED_OBJECT_ID = 0x03
SF_UNIQUE_OBJECT_ID                       = 0x86
SF_OPAQUE_STREAM                          = 0x40

# not sure about these
SF_DATA_VECTOR                            = 0xD2
SF_DATA_SET                               = 0xDA

PROPERTY_VERSION=32

class PropertyItem(object):
    def __init__(self, root, pid, format, version=PROPERTY_VERSION):
        self.root = root
        self.pid = pid
        self.format = format
        self.version = version
        self.data = None

    def format_name(self):
        return str(property_formats[self.format].__name__)

    @property
    def propertydef(self):
        classdef = self.root.classdef
        if classdef is None:
            return

        for p in classdef.all_propertydefs():
            if p.pid == self.pid:
                return p
    @property
    def name(self):
        propertydef = self.propertydef
        if propertydef:
            return propertydef.property_name

    @property
    def typedef(self):
        propertydef = self.propertydef
        if propertydef:
            return propertydef.typedef

    @property
    def value(self):
        try:
            return self.typedef.decode(self.data)
        except:
            print("0x%x" % self.format, "0x%04dx" % self.pid)
            print(self)
            print(self.root.dir.path())
            print(self.root.classdef)
            print(self.name, self.typedef, [self.data])
            raise
    @value.setter
    def value(self, value):
        self.data = self.typedef.encode(value)
        self.add_pid_entry()

    def add_pid_entry(self):
        if not self.pid in self.root.property_entries:
            self.root.property_entries[self.pid] = self


    def __repr__(self):
        return "0x%04X %s" % (self.pid, self.format_name())


class SFData(PropertyItem):
    def decode(self, data=None):
        self.data = data

    def __repr__(self):
        name = self.name
        if name:
            return "<%s %s>" % (name, str(self.typedef))
        else:
            return "<%s %d bytes>" % (self.__class__.__name__, len(self.data))

class SFStream(SFData):
    def decode(self, data=None):
        for i, c in enumerate(reversed(data)):
            if c != '\0':
                break

        self.stream_name = data[1:].decode("utf-16-le")

    def __repr__(self):
        return "<%s %s>" % (self.__class__.__name__, str(self.stream_name))

    @property
    def value(self):
        return self.root.dir.get(self.stream_name)


# abtract for refereneces
class SFObjectRef(SFData):
    pass

# abtract for referenece arrays
class SFObjectRefArray(SFObjectRef):
    pass

class SFStrongRef(SFObjectRef):
    def __init__(self, root, pid, format, version=PROPERTY_VERSION):
        super(SFStrongRef, self).__init__(root, pid, format, version)
        self.ref = None
        self.object = None

    def decode(self, data):
        self.data = data
        #null terminated
        self.ref = data[:-2].decode("utf-16le")

    def encode(self, data):
        return data.encode("utf-16le") + b"\x00" + b"\x00"

    def __repr__(self):
        return "<%s %s to %s>" % (self.name, self.__class__.__name__, str(self.ref))

    @property
    def value(self):
        if self.object:
            return self.object
        dir_entry = self.root.dir.get(self.ref)
        if dir_entry:
            self.object = self.root.root.read_object(dir_entry)
        return self.object


    @value.setter
    def value(self, value):

        typedef = self.typedef
        classdef = typedef.ref_classdef

        if value.classdef != classdef:
            raise Exception("must be instance of: %s" % classedef.class_name)

        if self.ref is None:
            propdef = self.propertydef
            self.ref = mangle_name(propdef.property_name, self.pid, 32)
            self.data = self.encode(self.ref)

        self.object = value
        if not self.pid in self.root.property_entries:
            self.root.property_entries[self.pid] = self

        # attach
        if self.root.dir:
            dir_entry = self.root.dir.get(self.ref)
            if dir_entry is None:
                dir_entry = self.root.dir.makedir(self.ref)

            value.attach(dir_entry)

# abtract for referenece arrays
class SFStrongRefArray(SFObjectRefArray):
    def encode(self, data):
        return data.encode("utf-16le") + b"\x00" + b"\x00"


class SFStrongRefVector(SFStrongRefArray):

    def __init__(self, root, pid, format, version=PROPERTY_VERSION):
        super(SFStrongRefVector, self).__init__(root, pid, format, version)
        self.references = []
        self.objects = []
        self.ref = None
        self.next_free_key = 0
        self.last_free_key = 0xFFFFFFFF

        # self.objects = {}
        self.local_map = {}


    def decode(self, data):
        self.references = []
        self.ref = None
        #null terminated
        self.ref = data[:-2].decode("utf-16le")
        self.objects = []

    # def read_index(self):
        if not self.ref:
            return

        index_name = self.ref + " index"
        index_dir = self.root.dir.get(index_name)
        if not index_dir:
            raise Exception()

        f = index_dir.open('r')
        count = read_u32le(f)
        self.next_free_key = read_u32le(f)
        self.last_free_key = read_u32le(f)

        for i in range(count):
            local_key = read_u32le(f)
            ref = "%s{%x}" % (self.ref, local_key)
            # print(i, count, ref)
            self.local_map[ref] = local_key
            self.references.append(ref)

    def write_index(self):
        f = self.root.dir.touch(self.ref + " index").open(mode='w')
        count = len(self.references)
        write_u32le(f, count)
        write_u32le(f, self.next_free_key)
        write_u32le(f, self.last_free_key)

        for ref in self.references:
            local_key = self.local_map[ref]
            write_u32le(f, local_key)

    @property
    def ref_classdef(self):
        return self.typedef.element_typedef.ref_classdef

    @property
    def value(self):
        if self.objects:
            return self.objects

        references = []
        for ref in self.references:
            dir_entry = self.root.dir.get(ref)
            item = self.root.root.read_object(dir_entry)
            references.append(item)

        self.objects = references
        return references

    @value.setter
    def value(self, value):

        self.objects = []
        ref_classdef = self.ref_classdef

        for obj in value:
            assert ref_classdef.isinstance(obj.classdef)

        if self.ref is None:
            propdef = self.propertydef
            self.ref = mangle_name(propdef.property_name, self.pid, 32-10)
            self.data = self.encode(self.ref)

        for obj in value:
            ref = "%s{%x}" % (self.ref, self.next_free_key)
            self.local_map[ref] = self.next_free_key
            self.references.append(ref)
            self.objects.append(obj)
            self.next_free_key += 1

        self.add_pid_entry()
        self.attach()

    def attach(self):
        # print("set attach")
        if not self.root.dir:
            return

        for i, ref in enumerate(self.references):

            obj = self.objects[i]
            # print(ref)
            dir_entry = self.root.dir.get(ref)
            if dir_entry is None:
                dir_entry = self.root.dir.makedir(ref)
            obj.attach(dir_entry)


    def __repr__(self):
        return "<%s %s to %s %d items>" % (self.name, self.__class__.__name__, str(self.ref), len(self.references))


class SFStrongRefSet(SFStrongRefArray):
    def __init__(self, root, pid, format, version=PROPERTY_VERSION):
        super(SFStrongRefSet, self).__init__(root, pid, format, version)
        self.references = {}
        self.ref = None
        self.objects = {}
        self.local_map = {}
        self.next_free_key = 0
        self.last_free_key = 0xFFFFFFFF
        self.key_size = 16
        # this pid match the ref_pid on the weak ref
        self.index_pid = 0

    def decode(self, data):
        self.data = data
        self.references = {}
        self.ref = None
        self.ref = data[:-2].decode("utf-16le")
        self.objects = {}
        self.local_map = {}


        if not self.ref:
            return

        index_name = self.ref + " index"
        index_dir = self.root.dir.get(index_name)
        if not index_dir:
            raise Exception()

        f = index_dir.open('r')
        count = read_u32le(f)
        self.next_free_key = read_u32le(f)
        self.last_free_key = read_u32le(f)
        self.index_pid = read_u16le(f)
        self.key_size = read_u8(f)

        # print("!!", self.propertydef.property_name,  "%x" % self.index_pid, self.next_free_key, self.last_free_key, "%x" % self.pid)

        # f = StringIO(f.read())

        for i in range(count):
            local_key = read_u32le(f)
            ref_count = read_u32le(f)

            # not sure if ref count is actually used
            # doesn't apear to be
            assert ref_count == 1

            key = f.read(self.key_size).encode("hex")
            ref = "%s{%x}" % (self.ref, local_key)
            self.local_map[key] = local_key
            self.references[key] = ref

    def write_index(self):
        f = self.root.dir.touch(self.ref + " index").open(mode='w')
        count = len(self.references)

        write_u32le(f, count)
        write_u32le(f, self.next_free_key)
        write_u32le(f, self.last_free_key)

        index_pid = self.root.root.metadict.weakref_pid(self.root.classdef, self.propertydef)
        # print("index_pid", index_pid)
        self.index_pid = index_pid
        # self.index_pid = self.root.classdef.weakref_pid
        write_u16le(f, self.index_pid)
        write_u8(f, self.key_size)

        for key, value in self.references.items():
            local_key = self.local_map[key]
            write_u32le(f, local_key)
            write_u32le(f, 1)
            f.write(key.decode("hex"))

    def get_object(self, key):
        if key in self.objects:
            return self.objects[key]

        ref = self.references[key]

        dir_entry = self.root.dir.get(ref)
        obj = self.root.root.read_object(dir_entry)
        self.objects[key] = obj
        return obj


    def items(self):

        for key, ref in self.references.items():
            obj = self.get_object(key)
            self.objects[key] = obj

            yield (key, obj)

    @property
    def value(self):

        if len(self.objects) == len(self.references):
            return self.objects
        d = {}
        for key, ref in self.items():
            d[key] = ref
        self.objects = d
        return d

    @value.setter
    def value(self, value):
        typedef = self.typedef
        classdef = typedef.ref_classdef

        if isinstance(value, list):
            d = {}
            for item in value:
                d[uuid.uuid4().hex] = item
            value = d

        for key, obj in value.items():

            if not classdef.isinstance(obj.classdef):
                raise Exception()

        self.objects = value

        if self.ref is None:
            propdef = self.propertydef
            self.ref = mangle_name(propdef.property_name, self.pid, 32-10)
            self.data = self.encode(self.ref)

        local_key = self.next_free_key
        for key, obj in value.items():
            ref = "%s{%x}" % (self.ref, local_key)
            self.local_map[key] = local_key
            self.references[key] = ref
            local_key += 1

        self.next_free_key = local_key

        self.add_pid_entry()
        self.attach()

    def attach(self):
        # print("set attach")
        if not self.root.dir:
            return

        for key, ref in self.references.items():
            obj = self.objects[key]
            dir_entry = self.root.dir.get(ref)
            if dir_entry is None:
                dir_entry = self.root.dir.makedir(ref)
            obj.attach(dir_entry)


    def __repr__(self):
        return "<%s to %s %d items>" % (self.__class__.__name__, str(self.ref), len(self.references))

class SFWeakRef(SFObjectRef):
    def __init__(self, root, pid, format, version=PROPERTY_VERSION):
        super(SFWeakRef, self).__init__(root, pid, format, version)
        self.ref_index = None
        self.ref_pid = None
        self.id_size = None
        self.ref = None

    def decode(self, data):
        self.data = data

        f = StringIO(data)

        self.ref_index = read_u16le(f)
        self.ref_pid = read_u16le(f)
        self.id_size = read_u8(f)
        self.ref = f.read(self.id_size).encode("hex")

    def encode(self):
        f = StringIO()

        ref = self.ref.decode('hex')
        id_size = len(ref)

        write_u16le(f, self.ref_index)
        write_u16le(f, self.ref_pid)
        write_u8(f, id_size)
        f.write(ref)
        return f.getvalue()

    def __repr__(self):
        return "<%s %s index %s %s>" % (self.name, self.__class__.__name__, self.ref_index, self.ref)

    @property
    def value(self):
        return self.root.root.resovle_weakref(self.ref_index, self.ref_pid, self.ref)

    @value.setter
    def value(self, value):

        (self.ref_index, self.ref_pid, self.ref)  = self.root.root.create_weakref(value, self.typedef.pid_path)

        self.data = self.encode()
        self.add_pid_entry()

class SFWeakRefArray(SFObjectRefArray):
    def __init__(self, root, pid, format, version=PROPERTY_VERSION):
        super(SFWeakRefArray, self).__init__(root, pid, format, version)
        self.references = []
        self.ref = None
        self.ref_index = None
        self.ref_pid = None
        self.id_size = None
        self.data = None

    def decode(self, data):

        self.data = data
        self.references = []

        #null terminated
        self.ref = data[:-2].decode("utf-16le")

        index_name = self.ref + " index"
        index_dir = self.root.dir.get(index_name)
        if not index_dir:
            raise Exception()

        f = index_dir.open('r')
        count = read_u32le(f)
        self.ref_index = read_u16le(f)
        self.ref_pid = read_u16le(f)
        self.id_size = read_u8(f)
        # print(self.pid)
        for i in range(count):
            identification = f.read(self.id_size).encode("hex")
            # print("  ",self.ref_index, identification)
            self.references.append(identification)

    def encode(self):
        return self.ref.encode("utf-16le") + "\x00" + "\x00"

    def write_index(self):
        f = self.root.dir.touch(self.ref + " index").open(mode='w')
        count = len(self.references)
        write_u32le(f, count)
        write_u16le(f, self.ref_index)
        write_u16le(f, self.ref_pid)
        write_u8(f, self.id_size)

        for item in self.references:
            f.write(item.decode('hex'))

    def __repr__(self):
        return "<%s %s to %d items>" % (self.name, self.__class__.__name__, len(self.references) )


    @property
    def value(self):
        items = []
        for ref in self.references:
            r = self.root.root.resovle_weakref(self.ref_index, self.ref_pid, ref)
            items.append(r)
        return items

    @value.setter
    def value(self, value):

        pid_path = self.typedef.element_typedef.pid_path

        if self.ref is None:
            propdef = self.propertydef
            self.ref = mangle_name(propdef.property_name, self.pid, 32)
            self.data = self.encode()

        for item in value:
            (ref_index, ref_pid, ref)  = self.root.root.create_weakref(item, pid_path)
            if self.ref_index is None:
                self.ref_index = ref_index
            if self.ref_pid is None:
                self.ref_pid = ref_pid
            if self.id_size is None:
                self.id_size = len(ref.decode('hex'))

            self.references.append(ref)


        self.add_pid_entry()

        # raise Exception()


class SFWeakRefVector(SFWeakRefArray):
    pass
class SFWeakRefSet(SFWeakRefArray):
    pass


# haven't see aaf files that contain these yet
class SFWeakRefId(SFWeakRef):
    pass

class SFUniqueId(SFData):
    pass

class SFOpaqueStream(SFData):
    pass

property_formats = {
SF_DATA                                    : SFData,
SF_DATA_STREAM                             : SFStream,
SF_STRONG_OBJECT_REFERENCE                 : SFStrongRef,
SF_STRONG_OBJECT_REFERENCE_VECTOR          : SFStrongRefVector,
SF_STRONG_OBJECT_REFERENCE_SET             : SFStrongRefSet,
SF_WEAK_OBJECT_REFERENCE                   : SFWeakRef,
SF_WEAK_OBJECT_REFERENCE_VECTOR            : SFWeakRefVector,
SF_WEAK_OBJECT_REFERENCE_SET               : SFWeakRefSet,
SF_WEAK_OBJECT_REFERENCE_STORED_OBJECT_ID  : SFWeakRefId,
SF_UNIQUE_OBJECT_ID                        : SFUniqueId,
SF_OPAQUE_STREAM                           : SFOpaqueStream
}