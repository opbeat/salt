'''
The client module is used to create a client connection to the publisher
The data structure needs to be:
    {'enc': 'clear',
     'load': {'fun': '<mod.callable>',
              'arg':, ('arg1', 'arg2', ...),
              'tgt': '<glob or id>',
              'key': '<read in the key file>'}
'''

# The components here are simple, and they need to be and stay simple, we
# want a client to have 3 external concerns, and maybe a forth configurable
# option.
# The concerns are:
# 1. Who executes the command?
# 2. What is the function being run?
# 3. What arguments need to be passed to the function?
# 4. How long do we wait for all of the replies?
#
# Next there are a number of tasks, first we need some kind of authentication
# This Client initially will be the master root client, which will run as
# the root user on the master server.
#
# BUT we also want a client to be able to work over the network, so that
# controllers can exist within disparate applications.
#
# The problem is that this is a security nightmare, so I am going to start
# small, and only start with the ability to execute salt commands locally.
# This means that the primary client to build is, the LocalClient

# Import python libs
import os
import glob
import time
import getpass

# Import salt libs
import salt.config
import salt.payload
import salt.utils
import salt.utils.verify
import salt.utils.event
import salt.utils.minions
from salt.exceptions import SaltInvocationError
from salt.exceptions import EauthAuthenticationError

# Try to import range from https://github.com/ytoolshed/range
RANGE = False
try:
    import seco.range
    RANGE = True
except ImportError:
    pass


def condition_kwarg(arg, kwarg):
    '''
    Return a single arg structure for the publisher to safely use
    '''
    if isinstance(kwarg, dict):
        kw_ = []
        for key, val in kwarg.items():
            kw_.append('{0}={1}'.format(key, val))
        return list(arg) + kw_
    return arg


class LocalClient(object):
    '''
    Connect to the salt master via the local server and via root
    '''
    def __init__(self, c_path='/etc/salt', mopts=None):
        if mopts:
            self.opts - mopts
        else:
            self.opts = salt.config.client_config(c_path)
        self.serial = salt.payload.Serial(self.opts)
        self.salt_user = self.__get_user()
        self.key = self.__read_master_key()
        self.event = salt.utils.event.MasterEvent(self.opts['sock_dir'])

    def __read_master_key(self):
        '''
        Read in the rotating master authentication key
        '''
        key_user = self.salt_user
        if key_user == 'root':
            if self.opts.get('user', 'root') != 'root':
                key_user = self.opts.get('user', 'root')
        if key_user.startswith('sudo_'):
            key_user = self.opts.get('user', 'root')
        keyfile = os.path.join(
                self.opts['cachedir'], '.{0}_key'.format(key_user)
                )
        # Make sure all key parent directories are accessible
        salt.utils.verify.check_parent_dirs(keyfile, key_user)

        try:
            with salt.utils.fopen(keyfile, 'r') as KEY:
                return KEY.read()
        except (OSError, IOError):
            # Fall back to eauth
            return ''

    def __get_user(self):
        '''
        Determine the current user running the salt command
        '''
        user = getpass.getuser()
        # if our user is root, look for other ways to figure out
        # who we are
        if user == 'root' or 'SUDO_USER' in os.environ:
            env_vars = ['SUDO_USER']
            for evar in env_vars:
                if evar in os.environ:
                    return 'sudo_{0}'.format(os.environ[evar])
            return user
        # If the running user is just the specified user in the
        # conf file, don't pass the user as it's implied.
        elif user == self.opts['user']:
            return user
        return user

    def _convert_range_to_list(self, tgt):
        range = seco.range.Range(self.opts['range_server'])
        try:
            return range.expand(tgt)
        except seco.range.RangeException as e:
            print(("Range server exception: {0}".format(e)))
            return []

    def gather_job_info(self, jid, tgt, tgt_type, **kwargs):
        '''
        Return the information about a given job
        '''
        return self.cmd(
                tgt,
                'saltutil.find_job',
                [jid],
                2,
                tgt_type,
                **kwargs)

    def _check_pub_data(self, pub_data):
        '''
        Common checks on the pub_data data structure returned from running pub
        '''
        if not pub_data:
            err = ('Failed to authenticate, is this user permitted to execute '
                   'commands?')
            raise EauthAuthenticationError(err)

        # Failed to connect to the master and send the pub
        if not 'jid' in pub_data or pub_data['jid'] == '0':
            return {}

        return pub_data

    def run_job(self,
            tgt,
            fun,
            arg=(),
            expr_form='glob',
            ret='',
            timeout=None,
            **kwargs):
        '''
        Prep the job dir and send minions the pub.
        Returns a dict of (checked) pub_data or an empty dict.
        '''
        try:
            jid = salt.utils.prep_jid(
                    self.opts['cachedir'],
                    self.opts['hash_type'],
                    user = __opts__['user']
                    )
        except Exception:
            jid = ''

        pub_data = self.pub(
            tgt,
            fun,
            arg,
            expr_form,
            ret,
            jid=jid,
            timeout=timeout or self.opts['timeout'],
            **kwargs)

        return self._check_pub_data(pub_data)

    def cmd(
        self,
        tgt,
        fun,
        arg=(),
        timeout=None,
        expr_form='glob',
        ret='',
        kwarg=None,
        **kwargs):
        '''
        Execute a salt command and return.
        '''
        arg = condition_kwarg(arg, kwarg)
        pub_data = self.run_job(
            tgt,
            fun,
            arg,
            expr_form,
            ret,
            timeout,
            **kwargs)

        if not pub_data:
            return pub_data

        return self.get_returns(pub_data['jid'], pub_data['minions'],
                timeout or self.opts['timeout'])

    def cmd_cli(
        self,
        tgt,
        fun,
        arg=(),
        timeout=None,
        expr_form='glob',
        ret='',
        verbose=False,
        kwarg=None,
        **kwargs):
        '''
        Execute a salt command and return data conditioned for command line
        output
        '''
        arg = condition_kwarg(arg, kwarg)
        pub_data = self.run_job(
            tgt,
            fun,
            arg,
            expr_form,
            ret,
            timeout,
            **kwargs)

        if not pub_data:
            yield pub_data
        else:
            for fn_ret in self.get_cli_event_returns(pub_data['jid'],
                    pub_data['minions'],
                    timeout or self.opts['timeout'],
                    tgt,
                    expr_form,
                    verbose,
                    **kwargs):

                if not fn_ret:
                    continue

                yield fn_ret

    def cmd_iter(
        self,
        tgt,
        fun,
        arg=(),
        timeout=None,
        expr_form='glob',
        ret='',
        kwarg=None,
        **kwargs):
        '''
        Execute a salt command and return an iterator to return data as it is
        received
        '''
        arg = condition_kwarg(arg, kwarg)
        pub_data = self.run_job(
            tgt,
            fun,
            arg,
            expr_form,
            ret,
            timeout,
            **kwargs)

        if not pub_data:
            yield pub_data
        else:
            for fn_ret in self.get_iter_returns(pub_data['jid'],
                    pub_data['minions'],
                    timeout or self.opts['timeout'],
                    tgt,
                    expr_form,
                    **kwargs):
                if not fn_ret:
                    continue
                yield fn_ret

    def cmd_iter_no_block(
        self,
        tgt,
        fun,
        arg=(),
        timeout=None,
        expr_form='glob',
        ret='',
        kwarg=None,
        **kwargs):
        '''
        Execute a salt command and return
        '''
        arg = condition_kwarg(arg, kwarg)
        pub_data = self.run_job(
            tgt,
            fun,
            arg,
            expr_form,
            ret,
            timeout,
            **kwargs)

        if not pub_data:
            yield pub_data
        else:
            for fn_ret in self.get_iter_returns(pub_data['jid'],
                    pub_data['minions'],
                    timeout):
                yield fn_ret

    def cmd_full_return(
        self,
        tgt,
        fun,
        arg=(),
        timeout=None,
        expr_form='glob',
        ret='',
        verbose=False,
        kwarg=None,
        **kwargs):
        '''
        Execute a salt command and return
        '''
        arg = condition_kwarg(arg, kwarg)
        pub_data = self.run_job(
            tgt,
            fun,
            arg,
            expr_form,
            ret,
            timeout,
            **kwargs)

        if not pub_data:
            return pub_data

        return (self.get_cli_static_event_returns(pub_data['jid'],
                    pub_data['minions'],
                    timeout,
                    tgt,
                    expr_form,
                    verbose))

    def get_cli_returns(
            self,
            jid,
            minions,
            timeout=None,
            tgt='*',
            tgt_type='glob',
            verbose=False,
            **kwargs):
        '''
        This method starts off a watcher looking at the return data for
        a specified jid, it returns all of the information for the jid
        '''
        if verbose:
            msg = 'Executing job with jid {0}'.format(jid)
            print(msg)
            print('-' * len(msg) + '\n')
        if timeout is None:
            timeout = self.opts['timeout']
        fret = {}
        inc_timeout = timeout
        jid_dir = salt.utils.jid_dir(
                jid,
                self.opts['cachedir'],
                self.opts['hash_type']
                )
        start = int(time.time())
        found = set()
        wtag = os.path.join(jid_dir, 'wtag*')
        # Check to see if the jid is real, if not return the empty dict
        if not os.path.isdir(jid_dir):
            yield {}
        # Wait for the hosts to check in
        while True:
            for fn_ in os.listdir(jid_dir):
                ret = {}
                if fn_.startswith('.'):
                    continue
                if fn_ not in found:
                    retp = os.path.join(jid_dir, fn_, 'return.p')
                    outp = os.path.join(jid_dir, fn_, 'out.p')
                    if not os.path.isfile(retp):
                        continue
                    while fn_ not in ret:
                        try:
                            check = True
                            ret_data = self.serial.load(salt.utils.fopen(retp, 'r'))
                            if ret_data is None:
                                # Sometimes the ret data is read at the wrong
                                # time and returns None, do a quick re-read
                                if check:
                                    check = False
                                    continue
                            ret[fn_] = {'ret': ret_data}
                            if os.path.isfile(outp):
                                ret[fn_]['out'] = self.serial.load(salt.utils.fopen(outp, 'r'))
                        except Exception:
                            pass
                    found.add(fn_)
                    fret.update(ret)
                    yield ret
            if glob.glob(wtag) and not int(time.time()) > start + timeout + 1:
                # The timeout +1 has not been reached and there is still a
                # write tag for the syndic
                continue
            if len(found.intersection(minions)) >= len(minions):
                # All minions have returned, break out of the loop
                break
            if int(time.time()) > start + timeout:
                # The timeout has been reached, check the jid to see if the
                # timeout needs to be increased
                jinfo = self.gather_job_info(jid, tgt, tgt_type, **kwargs)
                more_time = False
                for id_ in jinfo:
                    if jinfo[id_]:
                        if verbose:
                            print('Execution is still running on {0}'.format(id_))
                        more_time = True
                if more_time:
                    timeout += inc_timeout
                    continue
                if verbose:
                    if tgt_type == 'glob' or tgt_type == 'pcre':
                        if len(found.intersection(minions)) >= len(minions):
                            print('\nThe following minions did not return:')
                            fail = sorted(list(minions.difference(found)))
                            for minion in fail:
                                print(minion)
                break
            time.sleep(0.01)

    def get_iter_returns(
            self,
            jid,
            minions,
            timeout=None,
            tgt='*',
            tgt_type='glob',
            **kwargs):
        '''
        Watch the event system and return job data as it comes in
        '''
        if not isinstance(minions, set):
            if isinstance(minions, basestring):
                minions = set([minions])
            elif isinstance(minions, (list, tuple)):
                minions = set(list(minions))

        if timeout is None:
            timeout = self.opts['timeout']
        inc_timeout = timeout
        jid_dir = salt.utils.jid_dir(
                jid,
                self.opts['cachedir'],
                self.opts['hash_type']
                )
        start = int(time.time())
        found = set()
        wtag = os.path.join(jid_dir, 'wtag*')
        # Check to see if the jid is real, if not return the empty dict
        if not os.path.isdir(jid_dir):
            yield {}
        # Wait for the hosts to check in
        while True:
            raw = self.event.get_event(timeout, jid)
            if not raw is None:
                if 'syndic' in raw:
                    minions.update(raw['syndic'])
                    continue
                found.add(raw['id'])
                ret = {raw['id']: {'ret': raw['return']}}
                if 'out' in raw:
                    ret[raw['id']]['out'] = raw['out']
                yield ret
                if len(found.intersection(minions)) >= len(minions):
                    # All minions have returned, break out of the loop
                    break
                continue
            # Then event system timeout was reached and nothing was returned
            if len(found.intersection(minions)) >= len(minions):
                # All minions have returned, break out of the loop
                break
            if glob.glob(wtag) and not int(time.time()) > start + timeout + 1:
                # The timeout +1 has not been reached and there is still a
                # write tag for the syndic
                continue
            if int(time.time()) > start + timeout:
                # The timeout has been reached, check the jid to see if the
                # timeout needs to be increased
                jinfo = self.gather_job_info(jid, tgt, tgt_type, **kwargs)
                more_time = False
                for id_ in jinfo:
                    if jinfo[id_]:
                        more_time = True
                if more_time:
                    timeout += inc_timeout
                    continue
                break
            time.sleep(0.01)

    def get_returns(self, jid, minions, timeout=None):
        '''
        This method starts off a watcher looking at the return data for
        a specified jid
        '''
        if timeout is None:
            timeout = self.opts['timeout']
        jid_dir = salt.utils.jid_dir(
                jid,
                self.opts['cachedir'],
                self.opts['hash_type']
                )
        start = 999999999999
        gstart = int(time.time())
        ret = {}
        wtag = os.path.join(jid_dir, 'wtag*')
        # If jid == 0, there is no payload
        if int(jid) == 0:
            return ret
        # Check to see if the jid is real, if not return the empty dict
        if not os.path.isdir(jid_dir):
            return ret
        # Wait for the hosts to check in
        while True:
            for fn_ in os.listdir(jid_dir):
                if fn_.startswith('.'):
                    continue
                if fn_ not in ret:
                    retp = os.path.join(jid_dir, fn_, 'return.p')
                    if not os.path.isfile(retp):
                        continue
                    while fn_ not in ret:
                        try:
                            ret[fn_] = self.serial.load(salt.utils.fopen(retp, 'r'))
                        except Exception:
                            pass
            if ret and start == 999999999999:
                start = int(time.time())
            if glob.glob(wtag) and not int(time.time()) > start + timeout + 1:
                # The timeout +1 has not been reached and there is still a
                # write tag for the syndic
                continue
            if len(set(ret.keys()).intersection(minions)) >= len(minions):
                # All Minions have returned
                return ret
            if int(time.time()) > start + timeout:
                # The timeout has been reached
                return ret
            if int(time.time()) > gstart + timeout and not ret:
                # No minions have replied within the specified global timeout,
                # return an empty dict
                return ret
            time.sleep(0.02)

    def get_full_returns(self, jid, minions, timeout=None):
        '''
        This method starts off a watcher looking at the return data for
        a specified jid, it returns all of the information for the jid
        '''
        if timeout is None:
            timeout = self.opts['timeout']
        jid_dir = salt.utils.jid_dir(
                jid,
                self.opts['cachedir'],
                self.opts['hash_type']
                )
        start = 999999999999
        gstart = int(time.time())
        ret = {}
        wtag = os.path.join(jid_dir, 'wtag*')
        # Check to see if the jid is real, if not return the empty dict
        if not os.path.isdir(jid_dir):
            return ret
        # Wait for the hosts to check in
        while True:
            for fn_ in os.listdir(jid_dir):
                if fn_.startswith('.'):
                    continue
                if fn_ not in ret:
                    retp = os.path.join(jid_dir, fn_, 'return.p')
                    outp = os.path.join(jid_dir, fn_, 'out.p')
                    if not os.path.isfile(retp):
                        continue
                    while fn_ not in ret:
                        try:
                            ret_data = self.serial.load(salt.utils.fopen(retp, 'r'))
                            ret[fn_] = {'ret': ret_data}
                            if os.path.isfile(outp):
                                ret[fn_]['out'] = self.serial.load(salt.utils.fopen(outp, 'r'))
                        except Exception:
                            pass
            if ret and start == 999999999999:
                start = int(time.time())
            if glob.glob(wtag) and not int(time.time()) > start + timeout + 1:
                # The timeout +1 has not been reached and there is still a
                # write tag for the syndic
                continue
            if len(set(ret.keys()).intersection(minions)) >= len(minions):
                return ret
            if int(time.time()) > start + timeout:
                return ret
            if int(time.time()) > gstart + timeout and not ret:
                # No minions have replied within the specified global timeout,
                # return an empty dict
                return ret
            time.sleep(0.02)

    def get_cli_static_event_returns(
            self,
            jid,
            minions,
            timeout=None,
            tgt='*',
            tgt_type='glob',
            verbose=False):
        '''
        Get the returns for the command line interface via the event system
        '''
        minions = set(minions)
        if verbose:
            msg = 'Executing job with jid {0}'.format(jid)
            print(msg)
            print('-' * len(msg) + '\n')
        if timeout is None:
            timeout = self.opts['timeout']
        jid_dir = salt.utils.jid_dir(
                jid,
                self.opts['cachedir'],
                self.opts['hash_type']
                )
        start = int(time.time())
        found = set()
        ret = {}
        wtag = os.path.join(jid_dir, 'wtag*')
        # Check to see if the jid is real, if not return the empty dict
        if not os.path.isdir(jid_dir):
            return ret
        # Wait for the hosts to check in
        while True:
            raw = self.event.get_event(timeout, jid)
            if not raw is None:
                found.add(raw['id'])
                ret[raw['id']] = {'ret': raw['return']}
                if 'out' in raw:
                    ret[raw['id']]['out'] = raw['out']
                if len(found.intersection(minions)) >= len(minions):
                    # All minions have returned, break out of the loop
                    break
                continue
            # Then event system timeout was reached and nothing was returned
            if len(found.intersection(minions)) >= len(minions):
                # All minions have returned, break out of the loop
                break
            if glob.glob(wtag) and not int(time.time()) > start + timeout + 1:
                # The timeout +1 has not been reached and there is still a
                # write tag for the syndic
                continue
            if int(time.time()) > start + timeout:
                if verbose:
                    if tgt_type == 'glob' or tgt_type == 'pcre':
                        if not len(found) >= len(minions):
                            print('\nThe following minions did not return:')
                            fail = sorted(list(minions.difference(found)))
                            for minion in fail:
                                print(minion)
                break
            time.sleep(0.01)
        return ret

    def get_cli_event_returns(
            self,
            jid,
            minions,
            timeout=None,
            tgt='*',
            tgt_type='glob',
            verbose=False,
            **kwargs):
        '''
        Get the returns for the command line interface via the event system
        '''
        if not isinstance(minions, set):
            if isinstance(minions, basestring):
                minions = set([minions])
            elif isinstance(minions, (list, tuple)):
                minions = set(list(minions))

        if verbose:
            msg = 'Executing job with jid {0}'.format(jid)
            print(msg)
            print('-' * len(msg) + '\n')
        if timeout is None:
            timeout = self.opts['timeout']
        inc_timeout = timeout
        jid_dir = salt.utils.jid_dir(
                jid,
                self.opts['cachedir'],
                self.opts['hash_type']
                )
        start = int(time.time())
        found = set()
        wtag = os.path.join(jid_dir, 'wtag*')
        # Check to see if the jid is real, if not return the empty dict
        if not os.path.isdir(jid_dir):
            yield {}
        # Wait for the hosts to check in
        while True:
            raw = self.event.get_event(timeout, jid)
            if not raw is None:
                if 'syndic' in raw:
                    minions.update(raw['syndic'])
                    continue
                found.add(raw['id'])
                ret = {raw['id']: {'ret': raw['return']}}
                if 'out' in raw:
                    ret[raw['id']]['out'] = raw['out']
                yield ret
                if len(found.intersection(minions)) >= len(minions):
                    # All minions have returned, break out of the loop
                    break
                continue
            # Then event system timeout was reached and nothing was returned
            if len(found.intersection(minions)) >= len(minions):
                # All minions have returned, break out of the loop
                break
            if glob.glob(wtag) and not int(time.time()) > start + timeout + 1:
                # The timeout +1 has not been reached and there is still a
                # write tag for the syndic
                continue
            if int(time.time()) > start + timeout:
                # The timeout has been reached, check the jid to see if the
                # timeout needs to be increased
                jinfo = self.gather_job_info(jid, tgt, tgt_type, **kwargs)
                more_time = False
                for id_ in jinfo:
                    if jinfo[id_]:
                        if verbose:
                            print('Execution is still running on {0}'.format(id_))
                        more_time = True
                if more_time:
                    timeout += inc_timeout
                    continue
                if verbose:
                    if tgt_type == 'glob' or tgt_type == 'pcre':
                        if not len(found) >= len(minions):
                            print('\nThe following minions did not return:')
                            fail = sorted(list(minions.difference(found)))
                            for minion in fail:
                                print(minion)
                break
            time.sleep(0.01)

    def get_event_iter_returns(self, jid, minions, timeout=None):
        '''
        Gather the return data from the event system, break hard when timeout
        is reached.
        '''
        if timeout is None:
            timeout = self.opts['timeout']
        jid_dir = salt.utils.jid_dir(
                jid,
                self.opts['cachedir'],
                self.opts['hash_type']
                )
        found = set()
        # Check to see if the jid is real, if not return the empty dict
        if not os.path.isdir(jid_dir):
            yield {}
        # Wait for the hosts to check in
        while True:
            raw = self.event.get_event(timeout)
            if raw is None:
                # Timeout reached
                break
            found.add(raw['id'])
            ret = {raw['id']: {'ret': raw['return']}}
            if 'out' in raw:
                ret[raw['id']]['out'] = raw['out']
            yield ret
            time.sleep(0.02)

    def pub(self,
            tgt,
            fun,
            arg=(),
            expr_form='glob',
            ret='',
            jid='',
            timeout=5,
            **kwargs):
        '''
        Take the required arguments and publish the given command.
        Arguments:
            tgt:
                The tgt is a regex or a glob used to match up the ids on
                the minions. Salt works by always publishing every command
                to all of the minions and then the minions determine if
                the command is for them based on the tgt value.
            fun:
                The function name to be called on the remote host(s), this
                must be a string in the format "<modulename>.<function name>"
            arg:
                The arg option needs to be a tuple of arguments to pass
                to the calling function, if left blank
        Returns:
            jid:
                A string, as returned by the publisher, which is the job
                id, this will inform the client where to get the job results
            minions:
                A set, the targets that the tgt passed should match.
        '''
        # Make sure the publisher is running by checking the unix socket
        if not os.path.exists(
                os.path.join(
                    self.opts['sock_dir'],
                    'publish_pull.ipc'
                    )
                ):
            return {'jid': '0', 'minions': []}

        if expr_form == 'nodegroup':
            if tgt not in self.opts['nodegroups']:
                conf_file = self.opts.get('conf_file', 'the master config file')
                err = 'Node group {0} unavailable in {1}'.format(tgt, conf_file)
                raise SaltInvocationError(err)
            tgt = salt.utils.minions.nodegroup_comp(
                    tgt,
                    self.opts['nodegroups']
                    )
            expr_form = 'compound'

        # Convert a range expression to a list of nodes and change expression
        # form to list
        if expr_form == 'range' and RANGE:
            tgt = self._convert_range_to_list(tgt)
            expr_form = 'list'

        # If an external job cache is specified add it to the ret list
        if self.opts.get('ext_job_cache'):
            if ret:
                ret += ',{0}'.format(self.opts['ext_job_cache'])
            else:
                ret = self.opts['ext_job_cache']

        # format the payload - make a function that does this in the payload
        #   module
        # make the zmq client
        # connect to the req server
        # send!
        # return what we get back

        # Generate the standard keyword args to feed to format_payload
        payload_kwargs = {'cmd': 'publish',
                          'tgt': tgt,
                          'fun': fun,
                          'arg': arg,
                          'key': self.key,
                          'tgt_type': expr_form,
                          'ret': ret,
                          'jid': jid}

        # if kwargs are passed, pack them.
        if kwargs:
            payload_kwargs['kwargs'] = kwargs

        # If we have a salt user, add it to the payload
        if self.salt_user:
            payload_kwargs['user'] = self.salt_user

        # If we're a syndication master, pass the timeout
        if self.opts['order_masters']:
            payload_kwargs['to'] = timeout

        sreq = salt.payload.SREQ(
                'tcp://{0[interface]}:{0[ret_port]}'.format(self.opts),
                )
        payload = sreq.send('clear', payload_kwargs)
        if not payload:
            return payload
        return {'jid': payload['load']['jid'],
                'minions': payload['load']['minions']}


class FunctionWrapper(dict):
    '''
    Create a function wrapper that looks like the functions dict on the minion
    but invoked commands on the minion via a LocalClient.

    This allows SLS files to be loaded with an object that calls down to the
    minion when the salt functions dict is referenced.
    '''
    def __init__(self, opts, minion):
        self.opts = opts
        self.minion = minion
        self.local = LocalClient(self.opts['conf_file'])
        self.functions = self.__load_functions()

    def __missing__(self, key):
        '''
        Since the function key is missing, wrap this call to a command to the
        minion of said key if it is available in the self.functions set
        '''
        if not key in self.functions:
            raise KeyError
        return self.run_key(key)

    def __load_functions(self):
        '''
        Find out what functions are available on the minion
        '''
        return set(
                self.local.cmd(
                    self.minion,
                    'sys.list_functions'
                    ).get(self.minion, [])
                )

    def run_key(self, key):
        '''
        Return a function that executes the arguments passed via the local
        client
        '''
        def func(*args, **kwargs):
            '''
            Run a remote call
            '''
            args = list(args)
            for _key, _val in kwargs:
                args.append('{0}={1}'.format(_key, _val))
            return self.local.cmd(self.minion, key, args)

class Caller(object):
    '''
    Create an object used to call salt functions directly on a minion
    '''
    def __init__(self, c_path='/etc/salt/minion'):
        self.opts = salt.config.minion_config(c_path)
        self.sminion = salt.minion.SMinion(self.opts)

    def function(self, fun, *args, **kwargs):
        '''
        Call a single salt function
        '''
        func = self.sminion.functions[fun]
        args, kw = salt.minion.detect_kwargs(func, args, kwargs)
        return func(*args, **kw)
