from mr.developer import common
try:
    import xml.etree.ElementTree as etree
except ImportError:
    import elementtree.ElementTree as etree
import getpass
import os
import re
import subprocess
import sys

logger = common.logger


class SVNError(common.WCError):
    pass


class SVNAuthorizationError(SVNError):
    pass


class SVNCertificateError(SVNError):
    pass


class SVNCertificateRejectedError(SVNError):
    pass


class SVNWorkingCopy(common.BaseWorkingCopy):
    _svn_info_cache = {}
    _svn_auth_cache = {}
    _svn_cert_cache = {}

    def __init__(self, *args, **kwargs):
        common.BaseWorkingCopy.__init__(self, *args, **kwargs)
        self._svn_check_version()

    def _svn_check_version(self):
        try:
            cmd = subprocess.Popen(["svn", "--version"],
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        except OSError, e:
            if getattr(e, 'errno', None) == 2:
                logger.error("Couldn't find 'svn' executable in your PATH.")
                sys.exit(1)
            raise
        stdout, stderr = cmd.communicate()
        lines = stdout.split('\n')
        version = None
        if len(lines):
            version = re.search(r'(\d+)\.(\d+)(\.\d+)?', lines[0])
            if version is not None:
                version = version.groups()
                if len(version) == 3:
                    version = (int(version[0]), int(version[1]), int(version[2][1:]))
                else:
                    version = (int(version[0]), int(version[1]))
        if (cmd.returncode != 0) or (version is None):
            logger.error("Couldn't determine the version of 'svn' command.")
            logger.error("Subversion output:\n%s\n%s" % (stdout, stderr))
            sys.exit(1)
        if (version < (1, 5)):
            logger.error("The installed 'svn' command is too old, expected 1.5 or newer, got %s." % ".".join([str(x) for x in version]))
            sys.exit(1)

    def _svn_auth_get(self, url):
        for root in self._svn_auth_cache:
            if url.startswith(root):
                return self._svn_auth_cache[root]

    def _svn_accept_invalid_cert_get(self, url):
        for root in self._svn_cert_cache:
            if url.startswith(root):
                return self._svn_cert_cache[root]

    def _svn_error_wrapper(self, f, source, **kwargs):
        count = 4
        while count:
            count = count - 1
            try:
                return f(source, **kwargs)
            except SVNAuthorizationError, e:
                lines = e.args[0].split('\n')
                root = lines[-1].split('(')[-1].strip(')')
                before = self._svn_auth_cache.get(root)
                common.output_lock.acquire()
                common.input_lock.acquire()
                after = self._svn_auth_cache.get(root)
                if before != after:
                    count = count + 1
                    common.input_lock.release()
                    common.output_lock.release()
                    continue
                print "Authorization needed for '%s' at '%s'" % (source['name'], source['url'])
                user = raw_input("Username: ")
                passwd = getpass.getpass("Password: ")
                self._svn_auth_cache[root] = dict(
                    user=user,
                    passwd=passwd,
                )
                common.input_lock.release()
                common.output_lock.release()
            except SVNCertificateError, e:
                lines = e.args[0].split('\n')
                root = lines[-1].split('(')[-1].strip(')')
                before = self._svn_cert_cache.get(root)
                common.output_lock.acquire()
                common.input_lock.acquire()
                after = self._svn_cert_cache.get(root)
                if before != after:
                    count = count + 1
                    common.input_lock.release()
                    common.output_lock.release()
                    continue
                print "\n".join(lines[:-1])
                while 1:
                    answer = raw_input("(R)eject or accept (t)emporarily? ")
                    if answer.lower() in ['r','t']:
                        break
                    else:
                        print "Invalid answer, type 'r' for reject or 't' for temporarily."
                if answer == 'r':
                    self._svn_cert_cache[root] = False
                else:
                    self._svn_cert_cache[root] = True
                count = count + 1
                common.input_lock.release()
                common.output_lock.release()

    def _svn_checkout(self, source, **kwargs):
        name = source['name']
        path = source['path']
        url = source['url']
        args = ["svn", "checkout", url, path]
        stdout, stderr, returncode = self._svn_communicate(args, url, **kwargs)
        if returncode != 0:
            raise SVNError("Subversion checkout for '%s' failed.\n%s" % (name, stderr))
        if kwargs.get('verbose', False):
            return stdout

    def _svn_communicate(self, args, url, **kwargs):
        auth = self._svn_auth_get(url)
        if auth is not None:
            args[2:2] = ["--username", auth['user'],
                         "--password", auth['passwd']]
        if not kwargs.get('verbose', False):
            args[2:2] = ["--quiet"]
        accept_invalid_cert = self._svn_accept_invalid_cert_get(url)
        if accept_invalid_cert is True:
            args[2:2] = ["--trust-server-cert"]
        elif accept_invalid_cert is False:
            raise SVNCertificateRejectedError("Server certificate rejected by user")
        args[2:2] = ["--no-auth-cache"]
        interactive_args = args[:]
        args[2:2] = ["--non-interactive"]
        env = dict(os.environ)
        env['LC_ALL'] = 'C'
        cmd = subprocess.Popen(args, env=env,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
        stdout, stderr = cmd.communicate()
        if cmd.returncode != 0:
            lines = stderr.strip().split('\n')
            if 'authorization failed' in lines[-1]:
                raise SVNAuthorizationError(stderr.strip())
            if 'Server certificate verification failed: issuer is not trusted' in lines[-1]:
                cmd = subprocess.Popen(interactive_args, env=env,
                                       stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
                stdout, stderr = cmd.communicate('t')
                raise SVNCertificateError(stderr.strip())
        return stdout, stderr, cmd.returncode

    def _svn_info(self, source):
        name = source['name']
        if name in self._svn_info_cache:
            return self._svn_info_cache[name]
        path = source['path']
        cmd = subprocess.Popen(["svn", "info", "--non-interactive", "--xml",
                                path],
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
        stdout, stderr = cmd.communicate()
        if cmd.returncode != 0:
            raise SVNError("Subversion info for '%s' failed.\n%s" % (name, stderr))
        info = etree.fromstring(stdout)
        result = {}
        entry = info.find('entry')
        if entry is not None:
            rev = entry.attrib.get('revision')
            if rev is not None:
                result['revision'] = rev
            info_url = entry.find('url')
            if info_url is not None:
                result['url'] = info_url.text
        entry = info.find('entry')
        if entry is not None:
            root = entry.find('root')
            if root is not None:
                result['root'] = root.text
        self._svn_info_cache[name] = result
        return result

    def _svn_switch(self, source, **kwargs):
        name = source['name']
        path = source['path']
        url = source['url']
        args = ["svn", "switch", url, path]
        rev = source.get('revision', source.get('rev'))
        if rev is not None and not rev.startswith('>'):
            args.insert(2, '-r%s' % rev)
        stdout, stderr, returncode = self._svn_communicate(args, url, **kwargs)
        if returncode != 0:
            raise SVNError("Subversion switch for '%s' failed.\n%s" % (name, stderr))
        if kwargs.get('verbose', False):
            return stdout

    def _svn_update(self, source, **kwargs):
        name = source['name']
        path = source['path']
        url = source['url']
        args = ["svn", "update", path]
        rev = source.get('revision', source.get('rev'))
        if rev is not None and not rev.startswith('>'):
            args.insert(2, '-r%s' % rev)
        stdout, stderr, returncode = self._svn_communicate(args, url, **kwargs)
        if returncode != 0:
            raise SVNError("Subversion update for '%s' failed.\n%s" % (name, stderr))
        if kwargs.get('verbose', False):
            return stdout

    def svn_checkout(self, source, **kwargs):
        name = source['name']
        path = source['path']
        if os.path.exists(path):
            self.output((logger.info, "Skipped checkout of existing package '%s'." % name))
            return
        self.output((logger.info, "Checking out '%s' with subversion." % name))
        return self._svn_error_wrapper(self._svn_checkout, source, **kwargs)

    def svn_switch(self, source, **kwargs):
        name = source['name']
        self.output((logger.info, "Switching '%s' with subversion." % name))
        return self._svn_error_wrapper(self._svn_switch, source, **kwargs)

    def svn_update(self, source, **kwargs):
        name = source['name']
        self.output((logger.info, "Updating '%s' with subversion." % name))
        return self._svn_error_wrapper(self._svn_update, source, **kwargs)

    def checkout(self, source, **kwargs):
        name = source['name']
        path = source['path']
        update = self.should_update(source, **kwargs)
        if os.path.exists(path):
            matches = self.matches(source)
            if matches:
                if update:
                    self.update(source, **kwargs)
                else:
                    self.output((logger.info, "Skipped checkout of existing package '%s'." % name))
            else:
                if self.status(source) == 'clean':
                    return self.svn_switch(source, **kwargs)
                else:
                    raise SVNError("Can't switch package '%s' from '%s', because it's dirty." % (name, source['url']))
        else:
            return self.svn_checkout(source, **kwargs)

    def matches(self, source):
        info = self._svn_info(source)
        url = source['url']
        rev = info.get('revision')
        match = re.search('^(.+)@(\\d+)$', url)
        if match:
            url = match.group(1)
            rev = match.group(2)
        if 'rev' in source and 'revision' in source:
            raise ValueError("The source definition of '%s' contains duplicate revision option." % source['name'])
        elif ('rev' in source or 'revision' in source) and match:
            raise ValueError("The url of '%s' contains a revision and there is an additional revision option." % source['name'])
        elif 'rev' in source:
            rev = source['rev']
        elif 'revision' in source:
            rev = source['revision']
        if url.endswith('/'):
            url = url[:-1]
        if rev.startswith('>='):
            return (info.get('url') == url) and (int(info.get('revision')) >= int(rev[2:]))
        elif rev.startswith('>'):
            return (info.get('url') == url) and (int(info.get('revision')) > int(rev[1:]))
        else:
            return (info.get('url') == url) and (info.get('revision') == rev)

    def status(self, source, **kwargs):
        name = source['name']
        path = source['path']
        cmd = subprocess.Popen(["svn", "status", "--xml", path],
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
        stdout, stderr = cmd.communicate()
        if cmd.returncode != 0:
            raise SVNError("Subversion status for '%s' failed.\n%s" % (name, stderr))
        info = etree.fromstring(stdout)
        clean = True
        for target in info.findall('target'):
            for entry in target.findall('entry'):
                status = entry.find('wc-status')
                if status is not None and status.get('item') != 'external':
                    clean = False
                    break
        if clean:
            status = 'clean'
        else:
            status = 'dirty'
        if kwargs.get('verbose', False):
            cmd = subprocess.Popen(["svn", "status", path],
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
            stdout, stderr = cmd.communicate()
            if cmd.returncode != 0:
                raise SVNError("Subversion status for '%s' failed.\n%s" % (name, stderr))
            return status, stdout
        else:
            return status

    def update(self, source, **kwargs):
        name = source['name']
        force = kwargs.get('force', False)
        status = self.status(source)
        if not self.matches(source):
            if force or status == 'clean':
                return self.svn_switch(source, **kwargs)
            else:
                raise SVNError("Can't switch package '%s', because it's dirty." % name)
        if status != 'clean' and not force:
            raise SVNError("Can't update package '%s', because it's dirty." % name)
        return self.svn_update(source, **kwargs)

common.workingcopytypes['svn'] = SVNWorkingCopy
