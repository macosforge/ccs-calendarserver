##
# Copyright (c) 2005-2017 Apple Inc. All rights reserved.
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
from __future__ import print_function
from __future__ import with_statement

import gettext
import inspect
import os
import struct
import array
from locale import normalize

from twext.python.log import Logger
from pycalendar.duration import Duration

try:
    from osx.corefoundation import CFError, CFDataRef
    from osx.utils import CFLocaleRef, CFPropertyListRef
    foundationImported = True
except ImportError:
    foundationImported = False

from twistedcaldav.config import config

log = Logger()

"""
Localization module

How to use:

    from __future__ import with_statement
    from localization import translationTo

    with translationTo('de'):
        print(_("Hello"))
        print(_("The event will last %(days)d days") % { 'days' : 4 })

    ... Hallo
    ... Die Veranstaltung dauert 4 Tage

Before you can actually get translated text, you need to:

    1) Choose a "domain" for your code, such as 'calendarserver'
    2) Run xgettext on your source to generate a <domain>.po file.
    3) For each language, give the .po file to the person
       who is doing the translation for editing
    4) Run msgfmt.py on the translated .po to generate a binary .mo
    5) Put the .mo into locales/<lang>/LC_MESSAGES/<domain>.mo

The German .po file for the above example would look like:

    msgid "Hello"
    msgstr "Hallo"

    msgid "The event will last %(days)d days"
    msgstr "Die Veranstaltung dauert %(days)d Tage"

The transationTo class automatically binds '_' to the appropriate translation
function for the duration of the "with" context.  It's smart enough to allow
nesting of "with" contexts, as in:

    with translationTo('de'):
        print(_("Hello") # in German)

        with translationTo('fr'):
            print(_("Hello") # in French)

        print(_("Hello") # in German)

If a translation file cannot be found for the specified language, it will fall
back to 'en'.  If 'en' can't be found, gettext will raise IOError.

If you use the with/as form, you will get an object that implements some
helper methods for date formatting:

    with translationTo('en') as trans:
        print(trans.dtDate(DateTime.getToday()))

    ... Thursday, October 23, 2008

    with translationTo('fr') as trans:
        print(trans.dtDate(DateTime.getToday()))

    ... Jeudi, Octobre 23, 2008

The .po files contain localizable strings for month and day names, as well as
date format strings, in case a locale likes these values in a different order
or with different punctuation.


TODO: recurrence
"""


class translationTo(object):

    translations = {}

    def __init__(self, lang, domain='calendarserver', localeDir=None):

        if localeDir is None:
            localeDir = config.Localization.LocalesDirectory

        # Cache gettext translation objects in class.translations
        key = (lang, domain, localeDir)
        self.translation = self.translations.get(key, None)
        if self.translation is None:
            self.translation = gettext.translation(
                domain=domain,
                localedir=localeDir, languages=[lang, 'en'], fallback=True)
            self.translations[key] = self.translation

    def __enter__(self):
        # Get the caller's globals so we can rebind their '_' to our translator
        caller_globals = inspect.stack()[1][0].f_globals

        # Store whatever '_' is already bound to so we can restore it later
        if '_' in caller_globals:
            self.prev = caller_globals['_']

        # Rebind '_' to our translator
        caller_globals['_'] = self.translation.ugettext

        # What we return here is accessible to the caller via the 'as' clause
        return self

    def __exit__(self, type, value, traceback):
        # Restore '_' if it previously had a value
        if hasattr(self, 'prev'):
            inspect.stack()[1][0].f_globals['_'] = self.prev

        # Don't swallow exceptions
        return False

    def monthAbbreviation(self, monthNumber):
        return self.translation.ugettext(monthsAbbrev[monthNumber])

    def date(self, component):
        dtStart = component.propertyValue("DTSTART")
        return self.dtDate(dtStart)

    def time(self, component):
        """
        Examples:

        3:30 PM to 4:30 PM PDT
        All day
        3:30 PM PDT
        3:30 PM PDT to 7:30 PM EDT

        1 day
        2 days
        1 day 1 hour
        1 day 4 hours 18 minutes
        """

        # Bind to '_' so pygettext.py will pick this up for translation
        _ = self.translation.ugettext

        tzStart = tzEnd = None
        dtStart = component.propertyValue("DTSTART")
        if dtStart.isDateOnly():
            return ("", _("All day"))
        else:
            tzStart = dtStart.timeZoneDescriptor()

        dtEnd = component.propertyValue("DTEND")
        if dtEnd:
            if not dtEnd.isDateOnly():
                tzEnd = dtEnd.timeZoneDescriptor()
            duration = dtEnd - dtStart
        else:
            tzEnd = tzStart
            duration = component.propertyValue("DURATION")
            if duration:
                dtEnd = dtStart + duration
            else:
                if dtStart.isDateOnly():
                    dtEnd = None
                    duration = Duration(days=1)
                else:
                    dtEnd = dtStart + Duration(days=1)
                    dtEnd.setHHMMSS(0, 0, 0)
                    duration = dtEnd - dtStart

        if dtStart == dtEnd:
            return (self.dtTime(dtStart), "")

        return (
            _("%(startTime)s to %(endTime)s")
            % {
                'startTime': self.dtTime(
                    dtStart,
                    includeTimezone=(tzStart != tzEnd)
                ),
                'endTime': self.dtTime(dtEnd),
            },
            self.dtDuration(duration)
        )

    def dtDate(self, val):
        # Bind to '_' so pygettext.py will pick this up for translation
        _ = self.translation.ugettext

        return (
            _("%(dayName)s, %(monthName)s %(dayNumber)d, %(yearNumber)d")
            % {
                'dayName': _(daysFull[(val.getDayOfWeek() + 6) % 7]),
                'monthName': _(monthsFull[val.getMonth()]),
                'dayNumber': val.getDay(),
                'yearNumber': val.getYear(),
            }
        )

    def dtTime(self, val, includeTimezone=True):
        if val.isDateOnly():
            return ""

        # Bind to '_' so pygettext.py will pick this up for translation
        _ = self.translation.ugettext

        ampm = _("AM") if val.getHours() < 12 else _("PM")
        hour12 = val.getHours() % 12
        if hour12 == 0:
            hour12 = 12

        result = (
            _("%(hour12Number)d:%(minuteNumber)02d %(ampm)s")
            % {
                'hour24Number': val.getHours(),  # 0-23
                'hour12Number': hour12,  # 1-12
                'minuteNumber': val.getMinutes(),  # 0-59
                'ampm': ampm,
            }
        )

        if includeTimezone and val.local():
            result += " %s" % (val.timeZoneDescriptor(),)

        return result

    def dtDuration(self, val):

        # Bind to '_' so pygettext.py will pick this up for translation
        _ = self.translation.ugettext

        parts = []

        total = val.getTotalSeconds()
        days = total / (24 * 60 * 60)
        if days == 1:
            parts.append(_("1 day"))
        elif days > 1:
            parts.append(
                _("%(dayCount)d days") %
                {'dayCount': days}
            )

        hours = divmod(total / 3600, 24)[1]
        minutes = divmod(total / 60, 60)[1]
        seconds = divmod(total, 60)[1]

        if hours == 1:
            parts.append(_("1 hour"))
        elif hours > 1:
            parts.append(
                _("%(hourCount)d hours") %
                {'hourCount': hours}
            )

        if minutes == 1:
            parts.append(_("1 minute"))
        elif minutes > 1:
            parts.append(
                _("%(minuteCount)d minutes") %
                {'minuteCount': minutes}
            )

        if seconds == 1:
            parts.append(_("1 second"))
        elif seconds > 1:
            parts.append(
                _("%(secondCount)d seconds") %
                {'secondCount': seconds}
            )

        return " ".join(parts)


# The strings below are wrapped in _( ) for the benefit of pygettext.  We don't
# actually want them translated until they're used.

_ = lambda x: x

daysFull = [
    _("Monday"),
    _("Tuesday"),
    _("Wednesday"),
    _("Thursday"),
    _("Friday"),
    _("Saturday"),
    _("Sunday"),
]

daysAbbrev = [
    _("Mon"),
    _("Tue"),
    _("Wed"),
    _("Thu"),
    _("Fri"),
    _("Sun"),
    _("Sat"),
]

monthsFull = [
    "month is 1-based",
    _("January"),
    _("February"),
    _("March"),
    _("April"),
    _("May"),
    _("June"),
    _("July"),
    _("August"),
    _("September"),
    _("October"),
    _("November"),
    _("December"),
]

monthsAbbrev = [
    "month is 1-based",
    _("JAN"),
    _("FEB"),
    _("MAR"),
    _("APR"),
    _("MAY"),
    _("JUN"),
    _("JUL"),
    _("AUG"),
    _("SEP"),
    _("OCT"),
    _("NOV"),
    _("DEC"),
]


##
# String file conversion routines
##

def processLocalizationFiles(settings):

    lprojRoot = settings.TranslationsDirectory
    gnuRoot = settings.LocalesDirectory

    if not foundationImported:
        return

    # Do we have an Apple translations directory?
    if lprojRoot and gnuRoot and os.path.exists(lprojRoot):

        log.info("Looking for Apple .lproj directories in {r}", r=lprojRoot)

        # Make sure the gnutext translations directory exists
        if not os.path.exists(gnuRoot):
            try:
                os.mkdir(gnuRoot)
            except OSError:
                log.warn(
                    "Could not create gnuttext translation directory: {r}",
                    r=gnuRoot,
                )
                return

        # Scan for Apple translations (directories ending in .lproj)
        for item in os.listdir(lprojRoot):
            if item.endswith(".lproj"):
                stringsFile = os.path.join(
                    lprojRoot, item,
                    'calendarserver.strings'
                )
                localeName = normalize(item[:-6])
                moFile = os.path.join(
                    gnuRoot, localeName, 'LC_MESSAGES',
                    'calendarserver.mo'
                )
                if os.path.exists(stringsFile):
                    if (
                        not os.path.exists(moFile) or
                        os.stat(stringsFile).st_mtime >
                        os.stat(moFile).st_mtime
                    ):
                        log.info("Converting {s} to {m}", s=stringsFile, m=moFile)
                        try:
                            convertStringsFile(stringsFile, moFile)
                        except Exception as e:
                            log.error(
                                "Failed to convert {s} to {m}: {ex}",
                                s=stringsFile, m=moFile, ex=e,
                            )
                    else:
                        log.info("{m} is up to date", m=moFile)


class ParseError(Exception):
    pass


def convertStringsFile(src, dest):

    dir = os.path.dirname(dest)

    if not os.path.exists(dir):
        try:
            os.makedirs(dir)
        except OSError:
            # can't create directory to hold .po file
            return

    # Parse the binary plist .strings file:
    with open(src) as f:
        data = f.read()
    data = CFDataRef.fromString(data)
    try:
        parsed = CFPropertyListRef.createFromData(data)
        strings = parsed.toDict()
    except CFError as error:
        raise ParseError(error)

    # The format of GNUtext MO files is described here:
    # http://www.gnu.org/software/autoconf/manual/gettext/MO-Files.html

    originals = strings.keys()
    originals.sort()

    descriptors = []
    keys = ''
    values = ''

    for original in originals:
        translation = strings[original]

        if isinstance(original, unicode):
            original = original.encode("UTF-8")
        if isinstance(translation, unicode):
            translation = translation.encode("UTF-8")

        descriptors.append(
            (
                len(keys), len(original), len(values),
                len(translation)
            )
        )
        keys += original + '\0'  # <NUL> terminated
        values += translation + '\0'

    # The header is 28 bytes, each descriptor is 8 bytes, with two descriptors
    # per string (one pointing at original, one pointing at translation)
    keysOffset = 28 + len(originals) * 2 * 8
    valuesOffset = keysOffset + len(keys)

    keyDescriptors = []
    valueDescriptors = []
    for origOffset, origLen, transOffset, transLen in descriptors:
        keyDescriptors.append(origLen)
        keyDescriptors.append(keysOffset + origOffset)
        valueDescriptors.append(transLen)
        valueDescriptors.append(valuesOffset + transOffset)

    result = struct.pack(
        "Iiiiiii",
        0x950412DE,           # magic number
        0,                     # file format revision
        len(originals),        # number of strings
        28,                    # offset of table with original strings
        28 + len(originals) * 8,  # offset of table with translation strings
        0,                     # size of hashing table
        0                      # offset of hashing table
    )
    result += array.array("i", keyDescriptors).tostring()
    result += array.array("i", valueDescriptors).tostring()
    result += keys
    result += values

    with open(dest, "wb") as outFile:
        outFile.write(result)


def _remapLanguageCode(code):
    """
    Remap certain language codes to others, per the localization team
    """

    if code == "zh-Hans":  # Simplified Chinese
        code = "zh_CN"
    elif code == "zh-Hant":  # Traditional Chinese
        code = "zh_TW"

    return code


def getLanguage(config):
    """
    If the language has been specified explicitly in the config, return it.  Otherwise
    look it up via NSLocale on OS X.  Failing that, return "en"

    @param config: The configuration object to examine
    @type config: ConfigDict
    @return: The language code -- on OS X the supported ones are:
        de, en, es, fr, it, ja, ko, nl, zh_CN, zh_TW
    @rtype: C{str}
    """
    if config.Localization.Language:
        return config.Localization.Language

    try:
        language = CFLocaleRef.preferredLanguages()[0]
        language = _remapLanguageCode(language)
    except:
        language = "en"

    return language
