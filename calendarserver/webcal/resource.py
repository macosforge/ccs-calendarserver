##
# Copyright (c) 2009-2017 Apple Inc. All rights reserved.
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

"""
Calendar Server Web UI.
"""

__all__ = [
    "WebCalendarResource",
]

import os

from time import time
from urlparse import urlparse
from cgi import parse_qs

from twisted.internet.defer import succeed

from txweb2 import responsecode
from txweb2.http import Response
from txweb2.http_headers import MimeType
from txweb2.stream import MemoryStream
from txdav.xml import element as davxml
from txweb2.dav.resource import TwistedACLInheritable

from twistedcaldav.config import config
from twistedcaldav.extensions import DAVFile, ReadOnlyResourceMixIn
from twistedcaldav.timezones import hasTZ

DEFAULT_TIMEZONE = "America/Los_Angeles"


class WebCalendarResource (ReadOnlyResourceMixIn, DAVFile):

    def defaultAccessControlList(self):
        return succeed(
            davxml.ACL(
                davxml.ACE(
                    davxml.Principal(davxml.Authenticated()),
                    davxml.Grant(
                        davxml.Privilege(davxml.Read()),
                    ),
                    davxml.Protected(),
                    TwistedACLInheritable(),
                ),
            )
        )

    def etag(self):
        # Can't be calculated here
        return succeed(None)

    def contentLength(self):
        # Can't be calculated here
        return None

    def lastModified(self):
        return None

    def exists(self):
        return True

    def displayName(self):
        return "Web Calendar"

    def contentType(self):
        return MimeType.fromString("text/html; charset=utf-8")

    def contentEncoding(self):
        return None

    def createSimilarFile(self, path):
        return DAVFile(path, principalCollections=self.principalCollections())

    _htmlContent_lastCheck = 0
    _htmlContent_statInfo = 0
    _htmlContentDebug_lastCheck = 0
    _htmlContentDebug_statInfo = 0

    def htmlContent(self, debug=False):
        if debug:
            cacheAttr = "_htmlContentDebug"
            templateFileName = "debug_standalone.html"
        else:
            cacheAttr = "_htmlContent"
            templateFileName = "standalone.html"

        templateFileName = os.path.join(
            config.WebCalendarRoot, templateFileName
        )

        #
        # See if the file changed, and dump the cached template if so.
        # Don't bother to check if we've checked in the past minute.
        # We don't cache if debug is true.
        #
        if not debug and hasattr(self, cacheAttr):
            currentTime = time()
            if currentTime - getattr(self, cacheAttr + "_lastCheck") > 60:
                statInfo = os.stat(templateFileName)
                statInfo = (statInfo.st_mtime, statInfo.st_size)
                if statInfo != getattr(self, cacheAttr + "_statInfo"):
                    delattr(self, cacheAttr)
                    setattr(self, cacheAttr + "_statInfo", statInfo)
                setattr(self, cacheAttr + "_lastCheck", currentTime)

        #
        # If we don't have a cached template, load it up.
        #
        if not hasattr(self, cacheAttr):
            with open(templateFileName) as templateFile:
                htmlContent = templateFile.read()

            if debug:
                # Don't cache
                return htmlContent
            setattr(self, cacheAttr, htmlContent)

        return getattr(self, cacheAttr)

    def render(self, request):
        if not self.fp.isdir():
            return responsecode.NOT_FOUND

        #
        # Get URL of authenticated principal.
        # Don't need to authenticate here because the ACL will have already
        # required it.
        #
        authenticatedPrincipalURL = request.authnUser.principalURL()

        def queryValue(arg):
            query = parse_qs(urlparse(request.uri).query, True)
            return query.get(arg, [""])[0]

        #
        # Parse debug query arg
        #
        debug = queryValue("debug")
        debug = debug is not None and debug.lower() in ("1", "true", "yes")

        #
        # Parse TimeZone query arg
        #
        tzid = queryValue("tzid")
        if not tzid:
            tzid = getLocalTimezone()
            self.log.debug("Determined timezone to be {tzid}", tzid=tzid)

        #
        # Make some HTML
        #
        try:
            htmlContent = self.htmlContent(debug) % {
                "tzid": tzid,
                "principalURL": authenticatedPrincipalURL,
            }
        except IOError as e:
            self.log.error("Unable to obtain WebCalendar template: %s" % (e,))
            return responsecode.NOT_FOUND

        response = Response()
        response.stream = MemoryStream(htmlContent)

        for (header, value) in (
            ("content-type", self.contentType()),
            ("content-encoding", self.contentEncoding()),
        ):
            if value is not None:
                response.headers.setHeader(header, value)

        return response


try:
    from osx.utils import CFTimeZoneRef

    def lookupSystemTimezone():
        try:
            return CFTimeZoneRef.defaultTimeZoneName()
        except:
            return ""

except ImportError:
    def lookupSystemTimezone():
        return ""


def getLocalTimezone():
    """
    Returns the default timezone for the server.  The order of precedence is:
    config.DefaultTimezone, lookupSystemTimezone( ), DEFAULT_TIMEZONE.
    Also, if neither of the first two values in that list are in the timezone
    database, DEFAULT_TIMEZONE is returned.

    @return: The server's local timezone name
    @rtype: C{str}
    """
    if config.DefaultTimezone:
        if hasTZ(config.DefaultTimezone):
            return config.DefaultTimezone

    systemTimezone = lookupSystemTimezone()
    if hasTZ(systemTimezone):
        return systemTimezone

    return DEFAULT_TIMEZONE
