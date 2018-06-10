==========================
Getting Started
==========================

This is the core code base for the Calendar and Contacts Server, which is a CalDAV, CardDAV, WebDAV, and HTTP server.

For general information about the server, see https://apple.github.io/ccs-calendarserver.


==========================
Copyright and License
==========================

Copyright (c) 2005-2017 Apple Inc. All rights reserved.

This software is licensed under the Apache License, Version 2.0. The Apache License is a well-established open source license, enabling collaborative open source software development.

See the "LICENSE" file for the full text of the license terms.

==========================
QuickStart
==========================

**WARNING**: these instructions are for running a server from the source tree, which is useful for development. These are not the correct steps for running the server in deployment or as part of an OS install. You should not be using the run script in system startup files (eg. /etc/init.d); it does things (like download software) that you don't want to happen in that context.

Begin by creating a directory to contain Calendar and Contacts Server and all its dependencies::

 mkdir ~/CalendarServer
 cd CalendarServer

Next, check out the source code from the GIT repository. To check out the latest code::

 git clone https://github.com/apple/ccs-calendarserver.git

Note: if you have two-factor authentication activated on GitHub, you'll need to use a personal access token instead of your password.  You can generate personal access tokens at https://github.com/settings/tokens

`Pip <https://pip.pypa.io/en/stable/installing/>`_ is used to retrieve the python dependencies and stage them for use by virtualenv, however if your system does not have pip (and virtualenv), install it by running:

    python -m ensurepip

If this yields a permission denied error, you are likely using a system-wide installation of Python, so either retry with a user installation of Python or prefix the command with 'sudo'.

The server requires various external libraries in order to operate. The bin/develop script in the sources will retrieve these dependencies and install them to the .develop directory. Note that this behavior is currently also a side-effect of bin/run, but that is likely to change in the future::

    cd ccs-calendarserver
    ./bin/develop
    ____________________________________________________________

    Using system version of libffi.

    ____________________________________________________________

    Using system version of OpenLDAP.

    ____________________________________________________________

    Using system version of SASL.

    ____________________________________________________________
    ...

Before you can run the server, you need to set up a configuration file for development. There is a provided test configuration that you can use to start with, conf/caldavd-test.plist, which can be copied to conf/caldavd-dev.plist (the default config file used by the bin/run script). If conf/caldavd-dev.plist is not present when the server starts, you will be prompted to create a new one from conf/caldavd-test.plist.

You will need to choose a directory service to use to populate your server's principals (users, groups, resources, and locations). A directory service provides the Calendar and Contacts Server with information about these principals. The directory services supported by Calendar and Contacts Server are:

- XMLDirectoryService: this service is configurable via an XML file that contains principal information. The file conf/auth/accounts.xml provides an example principals configuration.
- OpenDirectoryService: this service uses Apple's OpenDirectory client, the bulk of the configuration for which is handled external to Calendar and Contacts Server (e.g. System Preferences --> Users & Groups --> Login Options --> Network Account Server).
- LdapDirectoryService: a highly flexible LDAP client that can leverage existing LDAP servers. See `twistedcaldav/stdconfig.py <https://github.com/apple/ccs-calendarserver/blob/master/twistedcaldav/stdconfig.py>`_ for the available LdapDirectoryService options and their defaults. 

The caldavd-test.plist configuration uses XMLDirectoryService by default, set up to use conf/auth/accounts-test.xml. This is a generally useful configuration for development and testing.

This file contains a user principal, named admin, with password admin, which is set up (in caldavd-test.plist) to have administrative permissions on the server.

Start the server using the bin/run script, and use the -n option to bypass dependency setup::

    bin/run -n 
    Using /Users/andre/CalendarServer/ccs-calendarserver/.develop/roots/py_modules/bin/python as Python

    Missing config file: /Users/andre/CalendarServer/ccs-calendarserver/conf/caldavd-dev.plist
    You might want to start by copying the test configuration:

      cp conf/caldavd-test.plist conf/caldavd-dev.plist

    Would you like to copy the test configuration now? [y/n]y
    Copying test cofiguration...

    Starting server...

The server should then start up and bind to port 8008 for HTTP and 8443 for HTTPS. You should then be able to connect to the server using your web browser (eg. Safari, Firefox) or with a CalDAV client (eg. Calendar).


==========================
Docker
==========================

A Dockerfile is provided to build an image that may be run in production.
An example docker-compose file is provided in contrib/docker.

PostgreSQL and Memcached are required as external services.

Configuration may be altered to make use of an LDAP server,
or XML files, as noted above.