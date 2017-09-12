from __future__ import (
    unicode_literals,
    absolute_import,
    print_function,
    division,
    )

import logging

import os
import uuid
import io
import math

from .utils import (
    read_u8, read_u16le,
    read_u32le, read_u64le,
    read_filetime, read_sid, read_uuid,
    write_u8, write_u16le,
    write_u32le, write_u64le,
    write_filetime, write_sid, write_uuid
)

from StringIO import StringIO

dir_types = {0x00 : 'empty',
             0x01 : 'storage',
             0x02 : 'stream',
             0x03 : 'lock bytes',
             0x04 : 'property',
             0x05 : 'root storage'}

DIFSECT    = 0xFFFFFFFC
FATSECT    = 0xFFFFFFFD
ENDOFCHAIN = 0xFFFFFFFE
FREESECT   = 0xFFFFFFFF

MAXREGSECT = 0xFFFFFFFA
MAXREGSID  = 0xFFFFFFFA
MAX_DIR_ENTRIES = 0x00FFFFFF

fat_sector_types = {DIFSECT    : "DIFSECT",
                    FATSECT    : "FATSECT",
                    ENDOFCHAIN : "ENDOFCHAIN",
                    FREESECT   : "FREESECT"}

def pretty_sectors(fat):
    return [fat_sector_types.get(item, item) for item in fat]

class Stream(object):

    def __init__(self, storage, entry, mode='r'):
        self.storage = storage
        self.dir = entry
        self.mode = mode
        self.buf = ""
        self.pos = 0
        if not mode in ('r', 'w'):
            raise Exception("invalid mode: %s" % mode)

    def tell(self):
        return self.pos

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_CUR:
            offset = self.tell() + offset
        elif whence == io.SEEK_END:
            offset = self.dir.byte_size + offset
        if offset < 0:
            raise ValueError('New position is before the start of the stream')

        if offset > self.dir.byte_size:
            logging.debug("overseek %d bytes, padding with zeros" % (offset - self.dir.byte_size))
            self.pos = self.dir.byte_size
            bytes_left = offset - self.dir.byte_size
            min_seek_size = self.storage.sector_size * 4
            while bytes_left:
                bytes_to_write = min(min_seek_size, offset - self.dir.byte_size)
                zeros = bytearray(bytes_to_write)
                self.write(zeros)
                bytes_left -= bytes_to_write

        self.pos = offset
        return offset

    def is_mini_stream(self):
        return self.dir.byte_size < self.storage.min_stream_max_size

    def sector_size(self):
        if self.is_mini_stream():
            return self.storage.mini_stream_sector_size
        else:
            return self.storage.sector_size

    def sector_offset(self):
        return self.pos % self.sector_size()

    def sector_index(self):
        return self.pos // self.sector_size()

    def abs_pos(self):
        minifat = self.is_mini_stream()

        start_sid = self.dir.sector_id

        sector_size = self.storage.sector_size
        mini_sector_size = self.storage.mini_stream_sector_size

        if minifat:
            minifat_chain = self.storage.iter_fat_chain(start_sid, True)
            mini_fat_index = self.pos // mini_sector_size
            sector_offset =  self.pos % mini_sector_size
            mini_stream_sid = minifat_chain[mini_fat_index]
            mini_steam_pos = (mini_stream_sid * mini_sector_size) + sector_offset

            index  = mini_steam_pos // sector_size
            offset = mini_steam_pos % sector_size
            sid = self.storage.mini_stream_chain[index]
            seek_pos = ((sid + 1) *  sector_size) + offset
            return seek_pos
        else:
            fat_chain = self.storage.iter_fat_chain(start_sid, False)
            index  = self.pos // sector_size
            offset = self.pos % sector_size

            sid = fat_chain[index]
            seek_pos = ((sid + 1) *  sector_size) + offset
            return seek_pos

    def read1(self, n=-1):
        if n == -1:
            n = max(0, self.dir.byte_size - self.tell())
        else:
            n = max(0, min(n, self.dir.byte_size - self.tell()))

        sector_size = self.sector_size()
        sector_offset = self.sector_offset()

        n = min(n, sector_size - sector_offset)
        if n == 0:
            return b''

        f = self.storage.f
        pos = self.abs_pos()
        f.seek(pos)
        result = f.read(n)
        self.pos += n
        return result

    def read(self, n=-1):
        if n == -1:
            n = max(0, self.dir.byte_size - self.tell())
        else:
            n = max(0, min(n, self.dir.byte_size - self.tell()))

        result = bytearray(n)
        i = 0
        while i < n:
            buf = self.read1(n - i)
            if not buf:
                logging.warn('file appears to be truncated')
                break
            result[i:i + len(buf)] = buf
            i += len(buf)

        return bytes(result)

    def write1(self, data):
        sector_size = self.sector_size()
        sector_offset = self.sector_offset()

        byte_writeable = min(len(data), sector_size - sector_offset)
        f = self.storage.f
        pos = self.abs_pos()
        f.seek(pos)
        logging.debug("write stream %d bytes at %d" % (byte_writeable, pos))
        f.write(data[:byte_writeable])
        self.pos += byte_writeable

        return byte_writeable

    def allocate(self, byte_size):

        minifat = self.is_mini_stream()
        realloc_data = None

        # convert from minifat to fat
        if minifat and byte_size >= self.storage.min_stream_max_size:
            logging.debug("converting stream for minifat to fat")
            self.seek(0)
            realloc_data = self.read()
            assert len(realloc_data) == self.dir.byte_size
            self.pos = 0
            self.storage.free_fat_chain(self.dir.sector_id, True)
            self.dir.sector_id = None
            minifat = False

        self.dir.byte_size = byte_size
        sector_count = int(math.ceil(byte_size / float(self.sector_size())))

        current_sects= len(self.storage.iter_fat_chain(self.dir.sector_id, minifat))
        logging.debug("%d bytes requires %d sectors at %d has %d" % (byte_size, sector_count, self.sector_size(), current_sects))

        while len(self.storage.iter_fat_chain(self.dir.sector_id, minifat)) < sector_count:
            sid = self.storage.fat_chain_append(self.dir.sector_id, minifat)
            if self.dir.sector_id is None:
                self.dir.sector_id = sid

        if not realloc_data is None:
            self.seek(0)
            self.write(realloc_data)

    def write(self, data):
        size = len(data)
        new_size = max(self.tell() + size, self.dir.byte_size)
        if new_size > self.dir.byte_size:
            self.allocate(new_size)

        while data:
            bytes_written = self.write1(data)
            data = data[bytes_written:]

        return size

    def close(self):
        pass

class DirEntry(object):
    def __init__(self, storage, dir_id):
        self.position = None
        self.name = None
        self.type = None
        self.color = 'black'

        self.left_id = None
        self.right_id = None
        self.child_id = None

        self.class_id = None
        self.flags = 0

        self.create_time = 0
        self.modify_time = 0

        self.sector_id = None
        self.byte_size = 0

        self.storage = storage
        self.dir_id = dir_id
        self.parent = None

    def __lt__(self, other):
        assert isinstance(other, DirEntry)

        if len(self.name) == len(other.name):
            # compare not case senstive
            assert self.name.upper() != other.name.upper()

            return self.name.upper() < other.name.upper()
        # shorter names are always less then
        else:
            return len(self.name) < len(other.name)

    def left(self):
        return self.storage.read_dir_entry(self.left_id, self.parent)

    def right(self):
        return self.storage.read_dir_entry(self.right_id, self.parent)

    def child(self):
        return self.storage.read_dir_entry(self.child_id, self)

    def add_child(self, entry):

        entry.parent = self
        child = self.child()
        if child:
            child.insert(entry)
        else:
            self.child_id = entry.dir_id

    def remove_child(self, entry):
        # NOTE: this is really ineffecient

        children = []
        for item in self.storage.listdir(self):
            if item.dir_id == entry.dir_id:
                continue
            children.append(item)

        self.child_id = None

        # construct a new child list
        for item in children:
            item.left_id = None
            item.right_id = None
            item.color = 'black'
            self.add_child(item)

    def insert(self, entry):

        root = self

        dir_per_sector = self.storage.sector_size // 128
        max_dirs_entries = self.storage.dir_sector_count * dir_per_sector

        count = 0

        # avoids recursion
        # while True:
        #     if count > max_dirs_entries:
        #         raise Exception("max dir entries limit reached")
        #
        #     if entry < root:
        #         left = root.left()
        #         if left:
        #             root = left
        #         else:
        #             root.left_id = entry.dir_id
        #             break
        #     else:
        #         right = root.right()
        #         if right:
        #             root = right
        #         else:
        #             root.right_id = entry.dir_id
        #             break
        #     count += 1

        # resucive version
        if entry < self:
            left = self.left()
            if left:
                left.insert(entry)
            else:
                self.left_id = entry.dir_id
        else:
            right = self.right()
            if right:
                right.insert(entry)
            else:
                self.right_id = entry.dir_id

    def path(self):
        path = []
        parent = self
        while parent:
            name = parent.name
            if name == "Root Entry":
                break
            path.append(parent.name)
            parent= parent.parent
        return  '/' + '/'.join(reversed(path))

    def open(self, mode='r'):
        if self.type != 'stream':
            raise Exception("can only open streams")
        return self.storage.open(self, mode)

    def isdir(self):
        return self.type in ('storage', 'root storage')

    def isroot(self):
        return self.type == 'root storage'

    def listdir(self):
        return self.storage.listdir(self)

    def makedir(self, relative_path, class_id = None):
        if not self.isdir():
            raise Exception()
        sep = '/'
        if self.isroot():
            sep = ''

        path = self.path() + sep + relative_path
        return self.storage.makedir(path, class_id)

    def isfile(self):
        return self.type == 'stream'

    def get(self, name, default=None):
        for item in self.listdir():
            if item.name.upper() == name.upper():
                return item
        return default

    def touch(self, name):
        item = self.get(name, None)
        if item:
            return item

        sep = '/'
        if self.isroot():
            sep = ''

        path = self.path() + sep + name
        return self.storage.create_dir_entry(path, 'stream', None)

    def write(self):
        f = self.storage.f
        f.seek(self.storage.dir_entry_pos(self.dir_id))

        self.position = f.tell()

        pos = f.tell()

        logging.debug("writing dir entry: %s id: %d type: %s at %d sid: %s bytes: %d" % (
                       self.path(), self.dir_id, self.type, pos, str(self.sector_id), self.byte_size))

        name_data = self.name.encode("utf-16le")
        name_size = len(name_data)
        assert name_size < 63

        f.write(name_data)
        pad = 64 - name_size
        while pad:
            f.write(b'\0')
            pad -= 1

        write_u16le(f, name_size+2)
        dir_type = 0x00
        for k,v in dir_types.items():
            if v == self.type:
                dir_type = k
                break

        write_u8(f, dir_type)
        write_u8(f, 0x00 if self.color == 'red' else 0x01)

        write_sid(f, self.left_id)
        write_sid(f, self.right_id)
        write_sid(f, self.child_id)

        write_uuid(f, self.class_id)
        write_u32le(f, self.flags)
        write_filetime(f , self.create_time)
        write_filetime(f , self.modify_time)

        write_sid(f, self.sector_id)
        write_u64le(f, self.byte_size)

        size = f.tell() - pos

        assert size == 128

    def read(self):
        f = self.storage.f
        f.seek(self.storage.dir_entry_pos(self.dir_id))
        self.position = f.tell()
        name_data = f.read(64)
        name_size = read_u16le(f)
        self.name  = name_data[:name_size-2].decode("utf-16le")

        dir_type = read_u8(f)
        color = read_u8(f)
        if color == 0x01:
            self.color = 'black'
        else:
            self.color = 'red'

        self.type = dir_types.get(dir_type , "unknown")
        self.left_id = read_sid(f)
        self.right_id = read_sid(f)
        self.child_id = read_sid(f)

        self.class_id = read_uuid(f)
        self.flags = read_u32le(f)
        self.create_time = read_filetime(f)
        self.modify_time = read_filetime(f)

        self.sector_id = read_sid(f)
        self.byte_size = read_u64le(f)

    def __repr__(self):
        return self.name

class CompoundFileBinary(object):
    def __init__(self, file_object):

        self.f = file_object

        self.difat = [[]]
        self.fat = []
        self.minifat = []

        self.difat_chain = []
        self.minifat_chain = []
        self.dir_fat_chain = []

        self.mini_stream_chain = []

        self.dir_cache = {}
        self.dir_free = set()

        self.debug_grow = False

        if isinstance(self.f, StringIO):
            self.mode = 'wb+'
        else:
            self.mode = self.f.mode


        if self.mode in ("r", "r+", "rb", 'rb+'):

            self.read_header()
            self.read_fat()
            self.read_minifat()

            # create dir_fat_chain and read root dir entry
            self.dir_fat_chain = self.iter_fat_chain(self.dir_sector_start)

            logging.debug("read %d dir sectors" % len(self.dir_fat_chain))
            self.root = self.read_dir_entry(0)
            self.dir_cache[0] = self.root

            # create mini stream fat chain
            if self.minifat_sector_count:
                self.mini_stream_chain = self.iter_fat_chain(self.root.sector_id)
        else:
            self.setup_empty()
            self.write_header()

            logging.debug("pos: %d" % self.f.tell())

            logging.debug("writing root dir sector")
            self.root.write()
            for i in range(self.sector_size - 128):
                self.f.write(b'\0')

            self.write_fat()

    def close(self):
        if self.mode in ("r", "rb"):
            return

        self.write_header()
        self.write_difat()
        self.write_fat()
        self.write_minifat()
        self.write_dir_entries()


    def setup_empty(self):

        self.class_id = uuid.UUID("0d010201-0200-0000-060e-2b3403020101")
        self.major_version = 4
        self.minor_version =  62

        self.byte_order = "le"

        self.sector_size = 4096
        self.mini_stream_sector_size = 64

        self.dir_sector_count = 1
        self.fat_sector_count = 1
        self.dir_sector_start = 0

        self.transaction_signature = 1
        self.min_stream_max_size = 4096

        self.minifat_sector_start = FREESECT
        self.minifat_sector_count = 0

        self.difat_sector_start = FREESECT
        self.difat_sector_count = 0

        self.difat = [[]]
        for i in range(109):
            self.difat[0].append(FREESECT)

        self.difat[0][0] = 1

        for i in range(self.sector_size // 4):
            self.fat.append(FREESECT)

        self.fat[0] = ENDOFCHAIN # end of dir chain
        self.fat[self.difat[0][0]] = FATSECT

        self.root = DirEntry(self, 0)
        self.root.name = 'Root Entry'
        self.root.sector_id = FREESECT
        self.root.type = 'root storage'
        self.root.class_id = uuid.UUID("b3b398a5-1c90-11d4-8053-080036210804")

        self.dir_cache[0] = self.root

        self.dir_fat_chain = [0]

        # raise NotImplementedError("mode: %s supported not implemented" % self.f.mode)

    def write_header(self):
        logging.debug("writiing header")
        f = self.f
        f.seek(0)
        f.write(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1') # Magic
        write_uuid(f, self.class_id)
        write_u16le(f, self.minor_version)
        write_u16le(f, self.major_version)
        write_u16le(f, 0xFFFE) #byte order le
        write_u16le(f, int(math.log(self.sector_size, 2)))
        write_u16le(f, int(math.log(self.mini_stream_sector_size, 2)))
        f.write(b'\0' * 6) #skip reseverd

        write_u32le(f, self.dir_sector_count)
        write_u32le(f, self.fat_sector_count)
        write_u32le(f, self.dir_sector_start)
        write_u32le(f, self.transaction_signature)
        write_u32le(f, self.min_stream_max_size)

        write_u32le(f, self.minifat_sector_start)
        write_u32le(f, self.minifat_sector_count)

        write_u32le(f, self.difat_sector_start)
        write_u32le(f, self.difat_sector_count)

        for i in range(109):
            write_u32le(f, self.difat[0][i])

        for i in range(self.sector_size - f.tell()):
            f.write(b'\0')


    def read_header(self):

        f = self.f
        f.seek(0)

        magic = f.read(8)
        # logging.debug("magic: %s" % magic.encode("hex"))
        logging.debug("magic: %s" % str([magic]))

        # clsid = f.read(16)
        # logging.debug("clsid: %s" % clsid.encode("hex"))
        self.class_id = read_uuid(f)
        logging.debug("clsid: %s" % str(self.class_id))

        self.minor_version = read_u16le(f)
        logging.debug("minor_version: %d" % self.minor_version)

        self.major_version = read_u16le(f)
        logging.debug("major_version: %d" % self.major_version)

        byte_order = read_u16le(f)
        if byte_order == 0xFFFE:
            self.byte_order = 'le'
        else:
            raise NotImplementedError("endian format:0x%X not supported" % byte_order)

        logging.debug("byte_order: %s" % self.byte_order)

        size = read_u16le(f)
        self.sector_size = pow(2, size)
        logging.debug("sector_size: %d -> %d" % (size, self.sector_size))

        size = read_u16le(f)
        self.mini_stream_sector_size = pow(2, size)
        logging.debug("mini_stream_sector_size: %d -> %d" % (size, self.mini_stream_sector_size))

        if not self.sector_size in (4096, 512):
            raise NotImplementedError("unsupported sector size: %d" % self.sector_size)
        if self.mini_stream_sector_size != 64:
            raise NotImplementedError("unsupported mini sector size: %d" % self.mini_stream_sector_size)

        f.read(6) #skip reseverd

        self.dir_sector_count = read_u32le(f)
        logging.debug("dir_sector_count: %d" % self.dir_sector_count)

        self.fat_sector_count = read_u32le(f)
        logging.debug("fat_sector_count: %d" % self.fat_sector_count)

        self.dir_sector_start = read_u32le(f)
        logging.debug("dir_sector_start: %d" % self.dir_sector_start)

        self.transaction_signature = read_u32le(f)
        logging.debug("transaction_signature: %d" % self.transaction_signature)

        self.min_stream_max_size = read_u32le(f)
        logging.debug("min_stream_max_size: %d" % self.min_stream_max_size)

        self.minifat_sector_start = read_u32le(f)
        logging.debug("minifat_sector_start: %d" % self.minifat_sector_start)

        self.minifat_sector_count = read_u32le(f)
        logging.debug("minifat_sector_count: %d" % self.minifat_sector_count)

        self.difat_sector_start = read_u32le(f)
        logging.debug("difat_sector_start: %d" % self.difat_sector_start)

        self.difat_sector_count = read_u32le(f)
        logging.debug("difat_sector_count: %d" % self.difat_sector_count)

        self.difat = [[]]

        logging.debug("reading header difat at %d" % f.tell())
        for i in range(109):
            item = read_u32le(f)
            # item = fat_sector_types.get(item, item)
            self.difat[0].append(item)

        sectors_left = self.difat_sector_count

        sid = self.difat_sector_start

        # reading difat sectors
        while sectors_left:
            logging.debug("reading difat sid: %d", sid)
            sector_type = fat_sector_types.get(sid, sid)
            if not isinstance(sector_type, int):
                break

            self.difat_chain.append(sid)
            f.seek((sid + 1) *  self.sector_size)
            difat = []
            for i in range( (self.sector_size // 4)):
                item = read_u32le(f)
                difat.append(item)
            self.difat.append(difat)

            sid = difat[-1]
            logging.debug("next difat: %d" % sid)
            sectors_left -= 1

    def iter_difat(self):
        for i, sid in enumerate(self.difat[0]):
            yield 0, i, sid

        t = 1
        for item in self.difat[1:]:
            for i, sid in enumerate(item[:-1]):
                yield t, i, sid
            t+=1


    def write_difat(self):
        f = self.f
        # write header entries
        f.seek(76)

        logging.debug("writing header difat")
        for i in range(109):
            write_u32le(f, self.difat[0][i])

        for i in range(self.sector_size - f.tell()):
            f.write(b'\0')

        if self.difat_sector_count == 0:
            return

        sid = self.difat_sector_start
        assert len(self.difat[1:]) == self.difat_sector_count
        for table in self.difat[1:]:

            sector_type = fat_sector_types.get(sid, sid)
            if not isinstance(sector_type, int):
                raise IOError("bad difat sector type")

            pos = (sid + 1) * self.sector_size
            logging.debug("writing difat to sid: %d at: %d" % (sid,pos))
            f.seek(pos)
            for i in range(self.sector_size // 4):
                write_u32le(f, table[i])

            sid = table[-1]

    def read_fat(self):
        f = self.f
        self.fat = []
        sector_count = 0
        fat_sectors = []
        for t, i, sid in self.iter_difat():

            sector_type = fat_sector_types.get(sid, sid)
            if not isinstance(sector_type, int):

                continue
            fat_sectors.append(sid)

        #  len(fat_sectors),self.fat_sector_count
        # assert len(fat_sectors) == self.fat_sector_count
        if len(fat_sectors) != self.fat_sector_count:
            logging.warn("fat sector count missmatch difat: %d header: %d" % (len(fat_sectors), self.fat_sector_count))
            self.fat_sector_count = len(fat_sectors)

        for sid in fat_sectors:

            pos = (sid + 1) *  self.sector_size
            # logging.debug("reading fat sid: %d pos: %d" % (sid, pos))
            # assert sector_count < self.fat_sector_count
            f.seek(pos)
            for j in range(self.sector_size // 4):
                try:
                    item = read_u32le(f)
                except:
                    sid
                    raise
                # item = fat_sector_types.get(item, item)
                self.fat.append(item)
            sector_count += 1

        logging.debug("read %d fat sectors ", sector_count)
        # logging.debug("fat: %s" % str(pretty_sectors(self.fat)))

    def write_fat(self):
        logging.debug("writing fat")
        f = self.f
        sector_count = 0

        assert len(self.fat)*4 % self.sector_size == 0

        fat_sectors = []

        for t, i, sid in self.iter_difat():
            sector_type = fat_sector_types.get(sid, sid)
            if not isinstance(sector_type, int):
                continue
            fat_sectors.append(sid)

        # check that the difat has enough entries to hold the current fat
        assert len(fat_sectors) == len(self.fat)*4 // self.sector_size

        for i, sid in enumerate(fat_sectors):

            logging.debug("writing fat to sid: %d" % sid)
            f.seek((sid + 1) *  self.sector_size)

            offset = i * self.sector_size // 4

            for j in range(self.sector_size // 4):
                write_u32le(f, self.fat[offset + j])

    def read_minifat(self):
        f = self.f
        sector_count = 0

        for sid in self.iter_fat_chain(self.minifat_sector_start):
            self.minifat_chain.append(sid)

            f.seek((sid + 1) *  self.sector_size)
            sector_count += 1
            for i in range(self.sector_size // 4):
                item = read_u32le(f)
                # item = fat_sector_types.get(item, item)
                self.minifat.append(item)
        logging.debug("read %d mini fat sectors", sector_count)

    def write_minifat(self):
        f = self.f
        sector_count = 0
        for i, sid in enumerate(self.iter_fat_chain(self.minifat_sector_start)):
            pos = (sid + 1) *  self.sector_size
            f.seek(pos)
            logging.debug("writing minifat to sid: %d at: %d" % (sid,pos))

            offset = i * self.sector_size // 4
            for j in range(self.sector_size // 4):
                write_u32le(f, self.minifat[offset + j])

            assert f.tell() - pos == self.sector_size

    def write_dir_entries(self):
        for dir_id, entry in self.dir_cache.items():
            entry.write()

        for dir_id in self.dir_free:
            # clear DirEntry
            f = self.f
            f.seek(self.dir_entry_pos(dir_id))
            logging.debug("clearing dir id: %d" % (dir_id))
            for i in range(128):
                f.write(b'\0')

    def next_free_minifat_sect(self):

        idx_per_sect = self.sector_size // self.mini_stream_sector_size
        stream_sects = len(self.mini_stream_chain) * idx_per_sect

        for i, item in enumerate(self.minifat):
            if item == FREESECT:
                if i+1 > stream_sects:
                    self.mini_stream_grow()
                return i
        # if we got here need to add aditional fat
        logging.debug("minifat full, growing")
        self.minifat_grow()
        return self.next_free_minifat_sect()

    def next_free_sect(self):

        for i, item in enumerate(self.fat):

            # hack to grow fat faster
            if self.debug_grow:
                size = len(self.fat)
                if i < size - 2:
                    continue

            if item == FREESECT:
                logging.debug("next free fat sect %d", i)
                return i

        # if we got here need to add aditional fat
        logging.debug("fat full, growing")

        difat_table = None
        difat_index = None

        for t, i, v in self.iter_difat():

            # hack to grow difat faster
            if self.debug_grow:
                size = len(self.difat[t])
                if i < size - 2:
                    continue

            if v == FREESECT:
                difat_table = t
                difat_index = i
                break

        new_difat_sect = None
        if difat_index is None:
            logging.debug("difat full, growing")
            new_difat_sect = len(self.fat) + 1
            logging.debug("adding new difat to sid: %d" % new_difat_sect)
            if self.difat_sector_count == 0:
                self.difat_sector_start = new_difat_sect
                self.difat_sector_count = 1
            else:
                self.difat[-1][-1] = new_difat_sect
                self.difat_sector_count += 1

            # add difat table
            difat = []
            for i in range(self.sector_size // 4):
                difat.append(FREESECT)

            difat[-1] == ENDOFCHAIN
            self.difat.append(difat)

            for t, i, v in self.iter_difat():
                if v == FREESECT:
                    difat_table = t
                    difat_index = i
                    break

        # TODO handle Range Lock Sector
        # The range lock sector is the sector
        # that covers file offsets 0x7FFFFF00-0x7FFFFFFF in the file

        new_fat_sect = len(self.fat)
        logging.debug("adding new fat to sid: %d" % new_fat_sect)

        self.difat[difat_table][difat_index] = new_fat_sect

        # grow fat entries
        for i in range(self.sector_size // 4):
            self.fat.append(FREESECT)

        self.fat[new_fat_sect] = FATSECT
        self.fat_sector_count += 1

        if not new_difat_sect is None:
            self.fat[new_difat_sect] = DIFSECT

        return self.next_free_sect()
        # raise NotImplementedError("adding additional fat")

    def dir_entry_pos(self, dir_id):
        stream_pos = dir_id * 128
        chain_index = stream_pos // self.sector_size
        sid_offset = stream_pos % self.sector_size
        sid = self.dir_fat_chain[chain_index]
        pos = ((sid + 1) *  self.sector_size) + sid_offset

        return pos

    def read_dir_entry(self, dir_id, parent = None):
        if dir_id is None:
            return None

        if dir_id in self.dir_cache:
            return self.dir_cache[dir_id]

        assert not dir_id in self.dir_free

        seek_pos = self.dir_entry_pos(dir_id)
        self.f.seek(seek_pos)

        entry = DirEntry(self, dir_id)
        entry.read()
        entry.parent = parent
        self.dir_cache[dir_id] = entry
        return entry

    def clear_sector(self, sid):
        sector_pos = (sid + 1) * self.sector_size
        self.f.seek(sector_pos)
        for i in range(self.sector_size):
            self.f.write(b'\0')

    def next_free_dir_id(self):

        # use free list first
        if self.dir_free:
            return self.dir_free.pop()

        f = self.f

        for i, sid in enumerate(self.dir_fat_chain):
            logging.debug("reading dir sector: %d" % sid)
            sector_pos = (sid + 1) *  self.sector_size

            sector_first_dir_id = i * (self.sector_size // 128)
            for j in range(self.sector_size // 128):
                dir_type_offset = 66
                offset = j * 128

                seek_pos = sector_pos + offset + dir_type_offset

                f.seek(seek_pos)
                dir_type = read_u8(f)
                if dir_types.get(dir_type , "unknown") == 'empty':
                    return sector_first_dir_id + j

        # if here we need to add to dir sector

        sect = self.fat_chain_append(self.dir_fat_chain[-1])

        self.dir_fat_chain.append(sect)
        self.dir_sector_count += 1
        self.clear_sector(sect)
        first_dir_id = (len(self.dir_fat_chain) - 1) * self.sector_size // 128
        for i in range(self.sector_size // 128):
            self.dir_free.add(first_dir_id + i)

        return self.next_free_dir_id()

    def valid_sector(self, sid):
        sector_type = fat_sector_types.get(sid, sid)
        if isinstance(sector_type, int):
            return True
        return False

    def iter_fat_chain(self, start_sid, minifat=False):
        fat = self.fat
        fat_name = "FAT"
        if minifat:
            fat = self.minifat
            fat_name = "MINIFAT"

        # Floyd's Tortoise and Hare cycle-finding algorithm
        a = start_sid
        b = start_sid
        sectors = []

        if not self.valid_sector(start_sid):
            return []

        while b != ENDOFCHAIN:
            sectors.append(b)
            b = fat[b]
            if a != ENDOFCHAIN:
                a = fat[a]
                if a != ENDOFCHAIN:
                    a = fat[a]
                    if a == b:
                        raise Exception('cyclic %s fat chain found starting at %d' % (fat_name, start_sid))

        return sectors

    def mini_stream_grow(self):
        sid = self.next_free_sect()
        logging.debug("adding to mini stream fat sid: %d" %  sid)
        if not self.mini_stream_chain:
            self.mini_stream_chain = [sid]
            self.root.sector_id = sid
        else:
            self.fat[self.mini_stream_chain[-1]] = sid
            self.mini_stream_chain.append(sid)

        # self.root.byte_size += self.sector_size
        self.fat[sid] = ENDOFCHAIN
        self.clear_sector(sid)

    def minifat_grow(self):
        # grow minifat
        sid = self.next_free_sect()
        logging.debug("growing minifat to sid %d" % sid)

        for i in range(self.sector_size // 4):
            self.minifat.append(FREESECT)

        if self.minifat_sector_count == 0:
            self.minifat_sector_count = 1
            self.minifat_sector_start = sid
        else:
            self.minifat_sector_count += 1
            self.fat[self.minifat_chain[-1]] = sid

        self.minifat_chain.append(sid)
        self.fat[sid] = ENDOFCHAIN

    def fat_chain_append(self, start_sid, minifat=False):

        if minifat:
            sect = self.next_free_minifat_sect()
            logging.debug("creating new mini sector: %d" % sect)
            fat = self.minifat
            self.root.byte_size += 64
        else:
            sect = self.next_free_sect()
            logging.debug("creating new sector: %d" % sect)
            fat = self.fat

        # fat[sect] = ENDOFCHAIN

        if start_sid is None:
            fat[sect] = ENDOFCHAIN
        else:
            fat_chain = self.iter_fat_chain(start_sid, minifat)
            fat[fat_chain[-1]] = sect
            fat[sect] = ENDOFCHAIN

        return sect

    def free_fat_chain(self, start_sid, minifat=False):
        fat =self.fat
        fat_name = "fat"
        if minifat:
            fat = self.minifat
            fat_name = "minifat"
        #  self.iter_fat_chain(start_sid, minifat)
        for sid in self.iter_fat_chain(start_sid, minifat):
            logging.debug("marking %s sid: %d as FREESECT" % (fat_name, sid))
            fat[sid] = FREESECT
            if minifat:
                self.root.byte_size -= 64

    def create_dir_entry(self, path, dir_type='storage', class_id=None):

        if self.exists(path):
            raise Exception("%s already exists" % path)

        dirname = os.path.dirname(path)
        basename = os.path.basename(path)

        root = self.find(dirname)

        if root is None:
            raise Exception("parent dirname does not exist: %s" % dirname)

        if not root.type in ('storage', 'root storage'):
            raise Exception("can not add entry to non storage type")

        dir_id = self.next_free_dir_id()
        logging.debug("next dir id %d" % dir_id)

        entry = DirEntry(self, dir_id)
        entry.name = basename
        entry.type = dir_type
        entry.class_id = class_id

        root.add_child(entry)
        self.dir_cache[dir_id] = entry
        entry.write()

        return entry

    def remove(self, path):
        """
        Removes both streams and storage DirEntry types from file.
        storage type entries need to be empty dirs.
        """

        entry = self.find(path)

        if not entry:
            raise Exception("%s does not exists" % path)

        if entry.type == 'root storage':
            raise Exception("can no remove root entry")

        if entry.type == "storage" and not entry.child_id is None:
            raise Exception("storage contains children")
        try:
            entry.parent.remove_child(entry)
        except:
            for item in self.listdir(entry.parent):
                 item
            raise

        # remove stream data
        if entry.type == "stream":
            self.free_fat_chain(entry.sector_id, entry.byte_size < self.min_stream_max_size)

        # add dir_id to free list
        self.dir_free.add(entry.dir_id)

        # remove from dir cache
        if entry.dir_id in self.dir_cache:
            del self.dir_cache[entry.dir_id]


    def rmtree(self, path):
        """
        Removes directory structure, similar to shutil.rmtree.
        """
        for root, storage, streams in self.walk(path, topdown=False):

            for item in streams:
                self.free_fat_chain(item.sector_id, item.byte_size < self.min_stream_max_size)
                self.dir_free.add(item.dir_id)
                if item.dir_id in self.dir_cache:
                    del self.dir_cache[item.dir_id]

            for item in storage:
                self.dir_free.add(item.dir_id)
                if item.dir_id in self.dir_cache:
                    del self.dir_cache[item.dir_id]

            root.child_id = None

        # remove root item
        self.remove(path)


    def listdir(self, path = None):
        """
        returns list DirEntries in path
        """

        if path is None:
            path = self.root

        root = self.find(path)
        if root is None:
            raise Exception("unable to find dir: %s" % str(path))

        if not root.isdir():
            raise Exception("can only list storage types")

        child = root.child()
        if not child:
            return []

        dir_per_sector = self.sector_size // 128
        max_dirs_entries = self.dir_sector_count * dir_per_sector
        count = 0

        # MorrisTraversal Non-recusive
        current = child

        result = []

        while not current is None:
            if current.left() is None:
                result.append(current)
                current = current.right()
            else:
                #Find the inorder predecessor of current
                pre = current.left()
                pre_count = 0
                while not pre.right() is None and pre.right() != current:
                    pre = pre.right()
                    pre_count += 1
                    # in case of inifinite loop, not sure if this can happen
                    # if pre_count > max_dirs_entries:
                    #     raise Exception("exceed max dir count %d %d" % (pre_count, max_dirs_entries))

                # Make current as right child of its inorder predecessor
                if pre.right() is None:
                    pre.right_id = current.dir_id
                    current = current.left()
                # Revert the changes made in if part to restore the original tree
                else:
                    pre.right_id = None
                    result.append(current)
                    current = current.right()

            count += 1
            # in case of inifinite loop, not sure if this can happen
            # if count > max_dirs_entries:
            #     raise Exception("exceed max dir count %d %d" % (count, max_dirs_entries))

        return result

    def find(self, path):

        if isinstance(path, DirEntry):
            return path

        if path == "/":
            return self.root

        split_path = path.lstrip('/').split("/")

        i = 0
        root = self.root

        while True:

            match = None
            for item in self.listdir(root):
                if item.name == split_path[i]:
                    match = item
                    break
            if match:
                if i == len(split_path) - 1:
                    return item
                root = match
                i += 1
            else:
                return None

    def walk(self, path = None, topdown=True):

        if path is None:
            path = self.root

        root = self.find(path)

        if not root.isdir():
            raise Exception("can only walk storage types")

        if not root.child_id:
            return

        if topdown:
            storage_items = []
            stream_items = []

            for item in self.listdir(root):
                if item.isdir():
                    storage_items.append(item)
                else:
                    stream_items.append(item)

            yield root, storage_items, stream_items

            for item in storage_items:
                for root, storage_items, stream_items in self.walk(item):
                    yield root, storage_items, stream_items
        else:

            def topdown_visit_node(root):
                storage_items = []
                stream_items = []
                for item in self.listdir(root):
                    if item.isdir():
                        for sub_root, sub_storage, sub_stream in topdown_visit_node(item):
                            yield sub_root, sub_storage, sub_stream

                        storage_items.append(item)
                    else:
                        stream_items.append(item)

                yield root, storage_items, stream_items

            for root_item, storage, stream in topdown_visit_node(root):
                yield root_item, storage, stream


    def exists(self, path):
        if self.find(path) is None:
            return False
        return True

    def makedir(self, path, class_id=None):
        return self.create_dir_entry(path, dir_type='storage', class_id=class_id)

    def open(self, path, mode='r'):
        """open stream."""

        entry = self.find(path)
        if entry is None:
            if mode == 'r':
                raise Exception("stream does not exists: %s" % path)
            entry = self.create_dir_entry(path, 'stream', None)

        else:
            if not entry.isfile():
                raise Exception("can only open stream type DirEntry's")

            if mode == 'w':
                logging.debug("stream: %s exists, overwriting" % path)
                self.free_fat_chain(entry.sector_id, entry.byte_size < self.min_stream_max_size)
                entry.sector_id = None
                entry.byte_size = 0
                entry.class_id = None

        s = Stream(self, entry, mode)
        return s