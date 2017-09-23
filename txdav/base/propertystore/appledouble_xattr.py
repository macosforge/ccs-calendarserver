# -*- test-case-name: txdav.base.propertystore.test.test_appledouble -*- ##
##
# Copyright (c) 2011-2017 Apple Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##


import struct

from txdav.base.propertystore.xattr import PropertyStore as XattrPropStore

# A lot of this is copied from python/plat-mac/applesingle.py,
# with data structure information taken from
# http://www.opensource.apple.com/source/Libc/Libc-391/darwin/copyfile.c

# File header format: magic, version, unused, number of entries
AS_HEADER_FORMAT = ">LL16sh"
AS_HEADER_LENGTH = 26

# The flag words for AppleDouble
AS_MAGIC = 0x00051607
AS_VERSION = 0x00020000

# Entry header format: id, offset, length
AS_ENTRY_FORMAT = ">lll"
AS_ENTRY_LENGTH = 12

# The id values
AS_DATAFORK = 1
AS_RESOURCEFORK = 2
AS_REALNAME = 3
AS_COMMENT = 4
AS_ICONBW = 5
AS_ICONCOLOR = 6
AS_DATESINFO = 8
AS_FINDERINFO = 9
AS_MACFILEINFO = 10
AS_PRODOSFILEINFO = 11
AS_MSDOSFILEINFO = 12
AS_SHORTNAME = 13
AS_AFPFILEINFO = 14
AS_DIECTORYID = 15

FINDER_INFO_LENGTH = 32
XATTR_OFFSET = FINDER_INFO_LENGTH + 2

XATTR_HDR_MAGIC = 0x41545452    # ATTR
XATTR_HEADER = ">llllllllhh"
XATTR_HEADER_LENGTH = struct.calcsize(XATTR_HEADER)
XATTR_ENTRY = ">llhb"
XATTR_ENTRY_LENGTH = struct.calcsize(XATTR_ENTRY)


def attrsFromFile(fileobj, debugFile=None):
    """
    Parse the extended attributes from a file.
    """

    attrs = {}

    # Get the top-level header
    header = fileobj.read(AS_HEADER_LENGTH)
    try:
        magic, version, _ignore, nentry = struct.unpack(
            AS_HEADER_FORMAT, header
        )
    except ValueError as arg:
        raise ValueError("Unpack header error: %s" % (arg,))
    if debugFile is not None:
        debugFile.write('Magic:   0x%8.8x\n' % (magic,))
        debugFile.write('Version: 0x%8.8x\n' % (version,))
        debugFile.write('Entries: %d\n' % (nentry,))
    if magic != AS_MAGIC:
        raise ValueError(
            "Unknown AppleDouble magic number 0x%8.8x" % (magic,)
        )
    if version != AS_VERSION:
        raise ValueError(
            "Unknown AppleDouble version number 0x%8.8x" % (version,)
        )
    if nentry <= 0:
        raise ValueError("AppleDouble file contains no forks")

    # Get each entry
    headers = [fileobj.read(AS_ENTRY_LENGTH) for _ignore_count in xrange(nentry)]
    for hdr in headers:
        try:
            restype, offset, length = struct.unpack(AS_ENTRY_FORMAT, hdr)
        except ValueError as arg:
            raise ValueError("Unpack entry error: %s" % (arg,))
        if debugFile is not None:
            debugFile.write("\n-- Fork %d, offset %d, length %d\n" %
                            (restype, offset, length))

        # Look for the FINDERINFO entry with extra bits
        if restype == AS_FINDERINFO and length > FINDER_INFO_LENGTH:

            # Get the xattr header
            fileobj.seek(offset + XATTR_OFFSET)
            data = fileobj.read(length - XATTR_OFFSET)
            if len(data) != length - XATTR_OFFSET:
                raise ValueError("Short read: expected %d bytes got %d" %
                                 (length - XATTR_OFFSET, len(data)))
            magic, _ignore_tag, total_size, data_start, data_length, \
                _ignore_reserved1, _ignore_reserved2, _ignore_reserved3, \
                flags, num_attrs = struct.unpack(XATTR_HEADER, data[:XATTR_HEADER_LENGTH])
            if magic != XATTR_HDR_MAGIC:
                raise ValueError("No xattrs found")

            if debugFile is not None:
                debugFile.write("\n  Xattr Header\n")
                debugFile.write('  Magic:       0x%08X\n' % (magic,))
                debugFile.write('  Total Size:  %d\n' % (total_size,))
                debugFile.write('  Data Start:  0x%02X\n' % (data_start,))
                debugFile.write('  Data Length: %d\n' % (data_length,))
                debugFile.write('  Flags:       0x%02X\n' % (flags,))
                debugFile.write('  Number:      %d\n' % (num_attrs,))

            # Get each xattr entry
            data = data[XATTR_HEADER_LENGTH:]
            for _ignore in xrange(num_attrs):
                [
                    xattr_offset, xattr_length,
                    xattr_flags, xattr_name_len
                ] = struct.unpack(XATTR_ENTRY, data[:XATTR_ENTRY_LENGTH])
                xattr_name = data[
                    XATTR_ENTRY_LENGTH:
                    XATTR_ENTRY_LENGTH + xattr_name_len - 1  # strip NULL terminator
                ]
                fileobj.seek(xattr_offset)
                xattr_value = fileobj.read(xattr_length)

                if debugFile is not None:
                    debugFile.write("\n    Xattr Entry\n")
                    debugFile.write('    Offset:      0x%02X\n' %
                                    (xattr_offset,))
                    debugFile.write('    Length:      %d\n' %
                                    (xattr_length,))
                    debugFile.write('    Flags:       0x%02X\n' %
                                    (xattr_flags,))
                    debugFile.write('    Name:        %r\n' % (xattr_name,))
                    debugFile.write('    Value:       %r\n' %
                                    (xattr_value,))
                attrs[xattr_name] = xattr_value

                # Skip over entry taking padding into account
                advance = (XATTR_ENTRY_LENGTH + xattr_name_len + 3) & ~3
                data = data[advance:]
    return attrs


class PropertyStore(XattrPropStore):
    """
    A property store that will read extended attributes from AppleDouble data in
    "._"-prefixed files alongside their data.

    Note that this is read-only but will not attempt to prevent write operations
    (since some operations attempt to transparently upgrade things by writing
    back properties, and we don't want to disrupt that operation).  Its only
    real purpose is to facilitate importing data with the file->database
    migration code.
    """

    # In this case, this prefix will _always_ be the MacOS prefix.  If you're
    # using this on some other OS, then you're importing some data from MacOS;
    # no Linux or BSD 'tar' will create AppleDouble files in the first place.

    deadPropertyXattrPrefix = "WebDAV:"

    @property
    def attrs(self):
        try:
            return attrsFromFile(self.path.sibling("._" + self.path.basename()).open())
        except IOError:
            return {}
