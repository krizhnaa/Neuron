#!/usr/bin/env python
# -*- coding: utf-8 -*-

# run with:
# sudo nohup /app/anaconda3/bin/python app.py > /dev/null 2>&1 &
from flask_cors import CORS
from typing import Union
from functools import wraps, partial
import os
import sys
import json
import secrets
import webbrowser
import time
import traceback
import pandas as pd
import threading
from queue import Queue
from waitress import serve  # necessary ?
from flask import Flask, url_for, redirect, jsonify, flash, send_from_directory
from flask import session, request, render_template
from flask import Response, stream_with_context
from satorilib.concepts.structs import StreamId, StreamOverviews
from satorilib.api.wallet.wallet import TransactionFailure
from satorilib.api.time import timestampToSeconds
from satorilib.api.wallet import RavencoinWallet, EvrmoreWallet
from satorilib.utils import getRandomName
from satorisynapse import Envelope
from satorineuron import VERSION, MOTO, config
from satorineuron import logging
from satorineuron.relay import acceptRelaySubmission, processRelayCsv, generateHookFromTarget, registerDataStream
from satorineuron.web import forms
from satorineuron.init.start import StartupDag
from satorineuron.web.utils import deduceCadenceString, deduceOffsetString

###############################################################################
## Globals ####################################################################
###############################################################################

# development flags
debug = True
darkmode = False
badForm = {}
app = Flask(__name__)
app.config['SECRET_KEY'] = secrets.token_urlsafe(16)
updateTime = 0
updateQueue = Queue()
CORS(app, origins=["https://satorinet.io"])


###############################################################################
## Startup ####################################################################
###############################################################################
MODE = os.environ.get('SATORI_RUN_MODE', 'dev')
while True:
    try:
        start = StartupDag(
            urlServer={
                'dev': 'http://localhost:5002',
                'prod': None,  # 'https://satorinet.io',
                'dockerdev': 'http://192.168.0.10:5002',
            }[MODE],
            urlPubsub={
                'dev': 'ws://localhost:3000',
                'prod': None,  # 'ws://satorinet.io:3000',
                'dockerdev': 'ws://192.168.0.10:3000',
            }[MODE],
            isDebug=sys.argv[1] if len(sys.argv) > 1 else False)
        threading.Thread(target=start.start, daemon=True).start()
        logging.info('Satori Neuron started!', color='green')
        break
    except ConnectionError as e:
        # try again...
        traceback.print_exc()
        logging.error(f'ConnectionError in app startup: {e}', color='red')
        time.sleep(30)
    # except RemoteDisconnected as e:
    except Exception as e:
        # try again...
        traceback.print_exc()
        logging.error(f'Exception in app startup: {e}', color='red')
        time.sleep(30)

###############################################################################
## Functions ##################################################################
###############################################################################


def returnNone():
    r = Response()
    # r.set_cookie("My important cookie", value=some_cool_value)
    return r, 204


def get_user_id():
    return session.get('user_id', '0')


def getFile(ext: str = '.csv') -> tuple[str, int, Union[None, 'FileStorage']]:
    if 'file' not in request.files:
        return 'No file uploaded', 400, None
    f = request.files['file']
    if f.filename == '':
        return 'No selected file', 400, None
    if f:
        if ext is None:
            return 'success', 200, f
        elif isinstance(ext, str) and f.filename.endswith(ext):
            return 'success', 200, f
        else:
            return 'Invalid file format. Only CSV files are allowed', 400, None
    return 'unknown error getting file', 500, None


def getResp(resp: Union[dict, None] = None) -> dict:
    return {
        'v': VERSION,
        'm': MOTO,
        'darkmode': darkmode,
        'title': 'Satori',
        **(resp or {})}


def closeVault(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        start.closeVault()
        return f(*args, **kwargs)
    return decorated_function


###############################################################################
## Errors #####################################################################
###############################################################################


@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404

###############################################################################
## Routes - static ############################################################
###############################################################################


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, 'static/img/favicon'),
        'favicon.ico',
        mimetype='image/vnd.microsoft.icon')


@app.route('/static/<path:path>')
def sendStatic(path):
    return send_from_directory('static', path)


@app.route('/generated/<path:path>')
def generated(path):
    return send_from_directory('generated', path)


@app.route('/upload_history_csv', methods=['POST'])
def uploadHistoryCsv():
    msg, status, f = getFile('.csv')
    if f is not None:
        f.save('/Satori/Neuron/uploaded/history.csv')
        return 'Successful upload.', 200
    else:
        flash(msg, 'success' if status == 200 else 'error')
    return redirect(url_for('dashboard'))


@app.route('/upload_datastream_csv', methods=['POST'])
def uploadDatastreamCsv():
    msg, status, f = getFile('.csv')
    if f is not None:
        df = pd.read_csv(f)
        processRelayCsv(start, df)
        logging.info('Successful upload', 200, print=True)
    else:
        logging.error(msg, status, print=True)
        flash(msg, 'success' if status == 200 else 'error')
    return redirect(url_for('dashboard'))


@app.route('/test')
def test():
    logging.info(request.MOBILE)
    return render_template('test.html')


@app.route('/kwargs')
def kwargs():
    ''' ...com/kwargs?0-name=widget_name0&0-value=widget_value0&0-type=widget_type0&1-name=widget_name1&1-value=widget_value1&1-#type=widget_type1 '''
    kwargs = {}
    for i in range(25):
        if request.args.get(f'{i}-name') and request.args.get(f'{i}-value'):
            kwargs[request.args.get(f'{i}-name')
                   ] = request.args.get(f'{i}-value')
            kwargs[request.args.get(f'{i}-name') +
                   '-type'] = request.args.get(f'{i}-type')
    return jsonify(kwargs)


@app.route('/ping', methods=['GET'])
def ping():
    from datetime import datetime
    return jsonify({'now': datetime.now().strftime("%Y-%m-%d %H:%M:%S")})


@app.route('/pause/<timeout>', methods=['GET'])
def pause(timeout):
    try:
        timeout = int(timeout)
        if timeout < 12:
            start.pause(timeout*60*60)
    except Exception as _:
        flash('invalid pause timeout', 'error')
    return redirect(url_for('dashboard'))


@app.route('/unpause', methods=['GET'])
def unpause():
    start.unpause()
    return redirect(url_for('dashboard'))


@app.route('/mode/light', methods=['GET'])
def modeLight():
    global darkmode
    darkmode = False
    return redirect(url_for('dashboard'))


@app.route('/mode/dark', methods=['GET'])
def modeDark():
    global darkmode
    darkmode = True
    return redirect(url_for('dashboard'))

###############################################################################
## Routes - forms #############################################################
###############################################################################


@app.route('/configuration', methods=['GET', 'POST'])
@closeVault
def editConfiguration():
    import importlib
    global forms
    forms = importlib.reload(forms)

    def present_form(edit_configuration):
        edit_configuration.flaskPort.data = config.flaskPort()
        edit_configuration.nodejsPort.data = config.nodejsPort()
        edit_configuration.dataPath.data = config.dataPath()
        edit_configuration.modelPath.data = config.modelPath()
        edit_configuration.walletPath.data = config.walletPath()
        edit_configuration.defaultSource.data = config.defaultSource()
        edit_configuration.electrumxServers.data = config.electrumxServers()
        return render_template('forms/config.html', **getResp({
            'title': 'Configuration',
            'edit_configuration': edit_configuration}))

    def accept_submittion(edit_configuration):
        data = {}
        if edit_configuration.flaskPort.data not in ['', None, config.flaskPort()]:
            data = {
                **data, **{config.verbose('flaskPort'): edit_configuration.flaskPort.data}}
        if edit_configuration.nodejsPort.data not in ['', None, config.nodejsPort()]:
            data = {
                **data, **{config.verbose('nodejsPort'): edit_configuration.nodejsPort.data}}
        if edit_configuration.dataPath.data not in ['', None, config.dataPath()]:
            data = {
                **data, **{config.verbose('dataPath'): edit_configuration.dataPath.data}}
        if edit_configuration.modelPath.data not in ['', None, config.modelPath()]:
            data = {
                **data, **{config.verbose('modelPath'): edit_configuration.modelPath.data}}
        if edit_configuration.walletPath.data not in ['', None, config.walletPath()]:
            data = {
                **data, **{config.verbose('walletPath'): edit_configuration.walletPath.data}}
        if edit_configuration.defaultSource.data not in ['', None, config.defaultSource()]:
            data = {
                **data, **{config.verbose('defaultSource'): edit_configuration.defaultSource.data}}
        if edit_configuration.electrumxServers.data not in ['', None, config.electrumxServers()]:
            data = {**data, **{config.verbose('electrumxServers'): [
                edit_configuration.electrumxServers.data]}}
        config.modify(data=data)
        return redirect('/dashboard')

    edit_configuration = forms.EditConfigurationForm(formdata=request.form)
    if request.method == 'POST':
        return accept_submittion(edit_configuration)
    return present_form(edit_configuration)


@app.route('/hook/<target>', methods=['GET'])
def hook(target: str = 'Close'):
    ''' generates a hook for the given target '''
    return generateHookFromTarget(target)


@app.route('/hook/', methods=['GET'])
def hookEmptyTarget():
    ''' generates a hook for the given target '''
    # in the case target is empty string
    return generateHookFromTarget('Close')


@app.route('/relay', methods=['POST'])
def relay():
    '''
    format for json post (as python dict):{
        "source": "satori",
        "name": "nameOfSomeAPI",
        "target": "optional",
        "data": 420,
    }
    '''

    # def accept_submittion(data: dict):
    #    if not start.relayValidation.valid_relay(data):
    #        return 'Invalid payload. here is an example: {"source": "satori", "name": "nameOfSomeAPI", "target": "optional", "data": 420}', 400
    #    if not start.relayValidation.stream_claimed(
    #        name=data.get('name'),
    #        target=data.get('target')
    #    ):
    #        save = start.relayValidation.register_stream(
    #            data=data)
    #        if save == False:
    #            return 'Unable to register stream with server', 500
    #        # get pubkey, recreate connection...
    #        start.checkin()
    #        start.pubsubConnect()
    #    # ...pass data onto pubsub
    #    start.pubsub.publish(
    #        topic=StreamId(
    #            source=data.get('source', 'satori'),
    #            author=start.wallet.publicKey,
    #            stream=data.get('name'),
    #            target=data.get('target')).topic(),
    #        data=data.get('data'))
    #    return 'Success: ', 200
    return acceptRelaySubmission(start, json.loads(request.get_json()))


@app.route('/send_satori_transaction_from_wallet/<network>', methods=['POST'])
def sendSatoriTransactionFromWallet(network: str = 'main'):
    return sendSatoriTransactionUsing(start.getWallet(network=network), network, 'wallet')


@app.route('/send_satori_transaction_from_vault/<network>', methods=['POST'])
def sendSatoriTransactionFromVault(network: str = 'main'):
    return sendSatoriTransactionUsing(start.vault, network, 'vault')


def sendSatoriTransactionUsing(myWallet: Union[RavencoinWallet, EvrmoreWallet], network: str, loc: str):
    if myWallet is None:
        flash(f'Send Failed: {e}')
        return redirect(f'/wallet/{network}')

    import importlib
    global forms
    global badForm
    forms = importlib.reload(forms)

    def accept_submittion(sendSatoriForm):
        def refreshWallet():
            time.sleep(4)
            myWallet.get(allWalletInfo=False)

        if sendSatoriForm.address.data == start.getWallet(network=network).address:
            # if we're sending to wallet we don't want it to auto send back to vault
            disableAutosecure(network)
        try:
            result = myWallet.typicalNeuronTransaction(
                sweep=sendSatoriForm.sweep.data,
                amount=sendSatoriForm.amount.data or 0,
                address=sendSatoriForm.address.data or '')
            if result.msg == 'creating partial, need feeSatsReserved.':
                responseJson = start.server.requestSimplePartial(
                    network=network)
                result = myWallet.typicalNeuronTransaction(
                    sweep=sendSatoriForm.sweep.data,
                    amount=sendSatoriForm.amount.data or 0,
                    address=sendSatoriForm.address.data or '',
                    completerAddress=responseJson.get('completerAddress'),
                    feeSatsReserved=responseJson.get('feeSatsReserved'),
                    changeAddress=responseJson.get('changeAddress'),
                )
            if result is None:
                flash('Send Failed: wait 10 minutes, refresh, and try again.')
            elif result.success:
                if (  # checking any on of these should suffice in theory...
                    result.tx is not None and
                    result.reportedFeeSats is not None and
                    result.reportedFeeSats > 0 and
                    result.msg == 'send transaction requires fee.'
                ):
                    r = start.server.broadcastSimplePartial(
                        tx=result.tx,
                        reportedFeeSats=result.reportedFeeSats,
                        feeSatsReserved=responseJson.get('feeSatsReserved'),
                        network=(
                            'ravencoin' if start.networkIsTest(network)
                            else 'evrmore'))
                    if r.text != '':
                        flash(r.text)
                    else:
                        flash(
                            'Send Failed: wait 10 minutes, refresh, and try again.')
                else:
                    flash(str(result.result))
            else:
                flash(f'Send Failed: {result.msg}')
        except TransactionFailure as e:
            flash(f'Send Failed: {e}')
        refreshWallet()
        return redirect(f'/{loc}/{network}')

    sendSatoriForm = forms.SendSatoriTransaction(formdata=request.form)
    return accept_submittion(sendSatoriForm)


@app.route('/register_stream', methods=['POST'])
def registerStream():
    import importlib
    global forms
    global badForm
    forms = importlib.reload(forms)

    def accept_submittion(newRelayStream):
        # done: we should register this stream and
        # todo: save the uri, headers, payload, and hook to a config manifest file.
        global badForm
        data = {
            # **({'source': newRelayStream.source.data} if newRelayStream.source.data not in ['', None] else {}), # in the future we will allow users to specify a source like streamr or satori
            **({'topic': newRelayStream.topic.data} if newRelayStream.topic.data not in ['', None] else {}),
            **({'name': newRelayStream.name.data} if newRelayStream.name.data not in ['', None] else {}),
            **({'target': newRelayStream.target.data} if newRelayStream.target.data not in ['', None] else {}),
            **({'cadence': newRelayStream.cadence.data} if newRelayStream.cadence.data not in ['', None] else {}),
            **({'offset': newRelayStream.offset.data} if newRelayStream.offset.data not in ['', None] else {}),
            **({'datatype': newRelayStream.datatype.data} if newRelayStream.datatype.data not in ['', None] else {}),
            **({'description': newRelayStream.description.data} if newRelayStream.description.data not in ['', None] else {}),
            **({'tags': newRelayStream.tags.data} if newRelayStream.tags.data not in ['', None] else {}),
            **({'url': newRelayStream.url.data} if newRelayStream.url.data not in ['', None] else {}),
            **({'uri': newRelayStream.uri.data} if newRelayStream.uri.data not in ['', None] else {}),
            **({'headers': newRelayStream.headers.data} if newRelayStream.headers.data not in ['', None] else {}),
            **({'payload': newRelayStream.payload.data} if newRelayStream.payload.data not in ['', None] else {}),
            **({'hook': newRelayStream.hook.data} if newRelayStream.hook.data not in ['', None] else {}),
            **({'history': newRelayStream.history.data} if newRelayStream.history.data not in ['', None] else {}),
        }
        if data.get('hook') in ['', None, {}]:
            hook, status = generateHookFromTarget(data.get('target', ''))
            if status == 200:
                data['hook'] = hook
        msgs, status = registerDataStream(start, data)
        if status == 400:
            badForm = data
        elif status == 200:
            badForm = {}
        for msg in msgs:
            flash(msg)
        return redirect('/dashboard')

    newRelayStream = forms.RelayStreamForm(formdata=request.form)
    return accept_submittion(newRelayStream)


@app.route('/edit_stream/<topic>', methods=['GET'])
def editStream(topic=None):
    # name,target,cadence,offset,datatype,description,tags,url,uri,headers,payload,hook
    import importlib
    global forms
    global badForm
    forms = importlib.reload(forms)
    try:
        badForm = [
            s for s in start.relay.streams
            if s.streamId.topic() == topic][0].asMap(noneToBlank=True)
    except IndexError:
        # on rare occasions
        # IndexError: list index out of range
        # cannot reproduce, maybe it's in the middle of reconnecting?
        pass
    # return redirect('/dashboard#:~:text=Create%20Data%20Stream')
    return redirect('/dashboard#CreateDataStream')


# @app.route('/remove_stream/<source>/<stream>/<target>/', methods=['GET'])
# def removeStream(source=None, stream=None, target=None):
@app.route('/remove_stream/<topic>', methods=['GET'])
def removeStream(topic=None):
    # removeRelayStream = {
    #    'source': source or 'satori',
    #    'name': stream,
    #    'target': target}
    removeRelayStream = StreamId.fromTopic(topic)
    return removeStreamLogic(removeRelayStream)


def removeStreamLogic(removeRelayStream: StreamId, doRedirect=True):
    def accept_submittion(removeRelayStream: StreamId, doRedirect=True):
        r = start.server.removeStream(payload=json.dumps({
            'source': removeRelayStream.source,
            # should match removeRelayStream.author
            'pubkey': start.wallet.publicKey,
            'stream': removeRelayStream.stream,
            'target': removeRelayStream.target,
        }))
        if (r.status_code == 200):
            msg = 'Stream deleted.'
            # get pubkey, recreate connection, restart relay engine
            try:
                start.relayValidation.claimed.remove(removeRelayStream)
            except Exception as e:
                logging.error(e)
            start.checkin()
            start.pubsubConnect()
            start.startRelay()
        else:
            msg = 'Unable to delete stream.'
        if doRedirect:
            flash(msg)
            return redirect('/dashboard')

    return accept_submittion(removeRelayStream, doRedirect)


@app.route('/remove_stream_by_post', methods=['POST'])
def removeStreamByPost():

    def accept_submittion(removeRelayStream):
        r = start.server.removeStream(payload=json.dumps({
            'source': removeRelayStream.get('source', 'satori'),
            'pubkey': start.wallet.publicKey,
            'stream': removeRelayStream.get('name'),
            'target': removeRelayStream.get('target'),
        }))
        if (r.status_code == 200):
            msg = 'Stream deleted.'
            # get pubkey, recreate connection, restart relay engine
            try:
                start.relayValidation.claimed.remove(removeRelayStream)
            except Exception as e:
                logging.error(e)
            start.checkin()
            start.pubsubConnect()
            start.startRelay()
        else:
            msg = 'Unable to delete stream.'
        flash(msg)
        return redirect('/dashboard')

    removeRelayStream = json.loads(request.get_json())
    return accept_submittion(removeRelayStream)
###############################################################################
## Routes - dashboard #########################################################
###############################################################################


@app.route('/', methods=['GET'])
@app.route('/home', methods=['GET'])
@app.route('/dashboard', methods=['GET'])
@closeVault
def dashboard():
    '''
    UI
    - send to setup process if first time running the app...
    - show earnings
    - access to wallet
    - access metrics for published streams
        (which streams do I have?)
        (how often am I publishing to my streams?)
    - access to data management (monitor storage resources)
    - access to model metrics
        (show accuracy over time)
        (model inputs and relative strengths)
        (access to all predictions and the truth)
    '''
    import importlib
    global forms
    global badForm
    forms = importlib.reload(forms)

    def present_stream_form():
        '''
        this function could be used to fill a form with the current
        configuration for a stream in order to edit it.
        '''
        if isinstance(badForm.get('streamId'), StreamId):
            name = badForm.get('streamId').stream
            target = badForm.get('streamId').target
        elif isinstance(badForm.get('streamId'), dict):
            name = badForm.get('streamId', {}).get('stream', '')
            target = badForm.get('streamId', {}).get('target', '')
        else:
            name = ''
            target = ''
        newRelayStream = forms.RelayStreamForm(formdata=request.form)
        newRelayStream.topic.data = badForm.get(
            'topic', badForm.get('kwargs', {}).get('topic', ''))
        newRelayStream.name.data = badForm.get('name', None) or name
        newRelayStream.target.data = badForm.get('target', None) or target
        newRelayStream.cadence.data = badForm.get('cadence', None)
        newRelayStream.offset.data = badForm.get('offset', None)
        newRelayStream.datatype.data = badForm.get('datatype', '')
        newRelayStream.description.data = badForm.get('description', '')
        newRelayStream.tags.data = badForm.get('tags', '')
        newRelayStream.url.data = badForm.get('url', '')
        newRelayStream.uri.data = badForm.get('uri', '')
        newRelayStream.headers.data = badForm.get('headers', '')
        newRelayStream.payload.data = badForm.get('payload', '')
        newRelayStream.hook.data = badForm.get('hook', '')
        newRelayStream.history.data = badForm.get('history', '')
        return newRelayStream

    # exampleStream = [Stream(streamId=StreamId(source='satori', author='self', stream='streamName', target='target'), cadence=3600, offset=0, datatype=None, description='example datastream', tags='example, raw', url='https://www.satorineuron.com', uri='https://www.satorineuron.com', headers=None, payload=None, hook=None, ).asMap(noneToBlank=True)]
    streamOverviews = (
        [model.miniOverview() for model in start.engine.models]
        if start.engine is not None else StreamOverviews.demo())
    return render_template('dashboard.html', **getResp({
        'wallet': start.wallet,
        'vaultBalanceAmount': start.vault.balanceAmount if start.vault is not None else 0,
        'streamOverviews': streamOverviews,
        'configOverrides': config.get(),
        'paused': start.paused,
        'newRelayStream': present_stream_form(),
        'shortenFunction': lambda x: x[0:15] + '...' if len(x) > 18 else x,
        'relayStreams':  # example stream +
        ([
            {
                **stream.asMap(noneToBlank=True),
                **{'latest': start.relay.latest.get(stream.streamId.topic(), '')},
                **{'late': start.relay.late(stream.streamId, timestampToSeconds(start.cacheOf(stream.streamId).getLatestObservationTime()))},
                **{'cadenceStr': deduceCadenceString(stream.cadence)},
                **{'offsetStr': deduceOffsetString(stream.offset)}}
            for stream in start.relay.streams]
         if start.relay is not None else []),

        'placeholderPostRequestHook': """def postRequestHook(response: 'requests.Response'): 
    '''
    called and given the response each time
    the endpoint for this data stream is hit.
    returns the value of the observaiton 
    as a string, integer or double.
    if empty string is returned the observation
    is not relayed to the network.
    '''                    
    if response.text != '':
        return float(response.json().get('Close', -1.0))
    return -1.0
""",
        'placeholderGetHistory': """class GetHistory(object):
    '''
    supplies the history of the data stream
    one observation at a time (getNext, isDone)
    or all at once (getAll)
    '''
    def __init__(self):
        super(GetHistory, self).__init__()

    def getNext(self):
        '''
        should return a value or a list of two values,
        the first being the time in UTC as a string of the observation,
        the second being the observation value
        '''
        return None

    def isDone(self):
        ''' returns true when there are no more observations to supply '''
        return None

    def getAll(self):
        ''' 
        if getAll returns a list or pandas DataFrame
        then getNext is never called
        '''
        return None

""",
    }))


@app.route('/pin_depin', methods=['POST'])
def pinDepinStream():
    # tell the server we want to toggle the pin of this stream
    # on the server that means mark the subscription as chosen by user
    # s = StreamId.fromTopic(request.data) # binary string actually works
    s = request.json
    payload = {
        'source': s.get('source', 'satori'),
        # 'pubkey': start.wallet.publicKey,
        'author': s.get('author'),
        'stream': s.get('stream', s.get('name')),
        'target': s.get('target'),
        # 'client': start.wallet.publicKey, # gets this from authenticated call
    }
    success, result = start.server.pinDepinStream(stream=payload)
    # return 'pinned' 'depinned' based on server response
    if success:
        return result, 200
    logging.error('pinDepinStream', s, success, result)
    return 'OK', 200


# old way
# @app.route('/model-updates')
# def modelUpdates():
#    def update():
#        global updating
#        if updating:
#            yield 'data: []\n\n'
#        logging.debug('modelUpdates', updating, color='yellow')
#        updating = True
#        streamOverviews = StreamOverviews(start.engine)
#        logging.debug('streamOverviews', streamOverviews, color='yellow')
#        listeners = []
#        # listeners.append(start.engine.data.newData.subscribe(
#        #    lambda x: streamOverviews.setIt() if x is not None else None))
#        if start.engine is not None:
#            logging.debug('start.engine is not None',
#                          start.engine is not None, color='yellow')
#            for model in start.engine.models:
#                listeners.append(model.predictionUpdate.subscribe(
#                    lambda x: streamOverviews.setIt() if x is not None else None))
#            while True:
#                logging.debug('in while loop', color='yellow')
#                if streamOverviews.viewed:
#                    logging.debug('NOT yeilding',
#                                  streamOverviews.viewed, color='yellow')
#                    time.sleep(60)
#                else:
#                    logging.debug('yeilding',
#                                  str(streamOverviews.overview).replace("'", '"'), color='yellow')
#                    # parse it out here?
#                    yield "data: " + str(streamOverviews.overview).replace("'", '"') + "\n\n"
#                    streamOverviews.setViewed()
#        else:
#            logging.debug('yeilding once', len(
#                str(streamOverviews.demo).replace("'", '"')), color='yellow')
#            yield "data: " + str(streamOverviews.demo).replace("'", '"') + "\n\n"
#
#    import time
#    return Response(update(), mimetype='text/event-stream')

@app.route('/model-updates')
def modelUpdates():
    def update():

        def on_next(model, x):
            global updateQueue
            if x is not None:
                overview = model.overview()
                # logging.debug('Yielding', overview, color='yellow')
                updateQueue.put(
                    "data: " + str(overview).replace("'", '"') + "\n\n")

        global updateTime
        global updateQueue
        listeners = []
        import time
        thisThreadsTime = time.time()
        updateTime = thisThreadsTime
        if start.engine is not None:
            for model in start.engine.models:
                # logging.debug('model', model.dataset.dropna(
                # ).iloc[-20:].loc[:, (model.variable.source, model.variable.author, model.variable.stream, model.variable.target)], color='yellow')
                listeners.append(
                    model.privatePredictionUpdate.subscribe(on_next=partial(on_next, model)))
            while True:
                data = updateQueue.get()
                if thisThreadsTime != updateTime:
                    return Response('data: oldCall\n\n', mimetype='text/event-stream')
                yield data
        else:
            # logging.debug('yeilding once', len(
            #     str(StreamOverviews.demo()).replace("'", '"')), color='yellow')
            yield "data: " + str(StreamOverviews.demo()).replace("'", '"') + "\n\n"

    return Response(update(), mimetype='text/event-stream')


@app.route('/working_updates')
def workingUpdates():
    def update():
        try:
            yield 'data: \n\n'
            messages = []
            listeners = []
            listeners.append(start.workingUpdates.subscribe(
                lambda x: messages.append(x) if x is not None else None))
            while True:
                time.sleep(1)
                if len(messages) > 0:
                    msg = messages.pop(0)
                    if msg == 'working_updates_end':
                        break
                    yield "data: " + str(msg).replace("'", '"') + "\n\n"
        except Exception as e:
            logging.error('working_updates error:', e, print=True)

    import time
    return Response(update(), mimetype='text/event-stream')


@app.route('/working_updates_end')
def workingUpdatesEnd():
    start.workingUpdates.on_next('working_updates_end')
    return 'ok', 200


@app.route('/remove_wallet_alias/<network>')
def removeWalletAlias(network: str = 'main', alias: str = ''):
    myWallet = start.getWallet(network=network)
    myWallet.get(allWalletInfo=False)
    myWallet.setAlias(None)
    start.server.removeWalletAlias()
    return render_template('wallet-page.html', **getResp({
        'title': 'Wallet',
        'walletIcon': 'wallet',
        'network': network,
        'image': getQRCode(myWallet.address),
        'wallet': myWallet,
        'exampleAlias': getRandomName(),
        'alias': '',
        'sendSatoriTransaction': presentSendSatoriTransactionform(request.form)}))


@app.route('/update_wallet_alias/<network>/<alias>')
def updateWalletAlias(network: str = 'main', alias: str = ''):
    myWallet = start.getWallet(network=network)
    myWallet.get(allWalletInfo=False)
    myWallet.setAlias(alias)
    start.server.updateWalletAlias(alias)
    return render_template('wallet-page.html', **getResp({
        'title': 'Wallet',
        'walletIcon': 'wallet',
        'network': network,
        'image': getQRCode(myWallet.address),
        'wallet': myWallet,
        'exampleAlias': getRandomName(),
        'alias': alias,
        'sendSatoriTransaction': presentSendSatoriTransactionform(request.form)}))


@app.route('/wallet/<network>')
@closeVault
def wallet(network: str = 'main'):
    myWallet = start.getWallet(network=network)
    myWallet.get(allWalletInfo=False)
    alias = myWallet.alias or start.server.getWalletAlias()
    return render_template('wallet-page.html', **getResp({
        'title': 'Wallet',
        'walletIcon': 'wallet',
        'network': network,
        'image': getQRCode(myWallet.address),
        'wallet': myWallet,
        'exampleAlias': getRandomName(),
        'alias': alias,
        'sendSatoriTransaction': presentSendSatoriTransactionform(request.form)}))


def getQRCode(value: str) -> str:
    import io
    import qrcode
    from base64 import b64encode
    img = qrcode.make(value)
    buf = io.BytesIO()
    img.save(buf)
    buf.seek(0)
    # return send_file(buf, mimetype='image/jpeg')
    img = b64encode(buf.getvalue()).decode('ascii')
    return f'<img src="data:image/jpg;base64,{img}" class="img-fluid"/>'


def presentSendSatoriTransactionform(formData):
    '''
    this function could be used to fill a form with the current
    configuration for a stream in order to edit it.
    '''
    # not sure if this part is necessary
    global forms
    import importlib
    forms = importlib.reload(forms)
    sendSatoriTransaction = forms.SendSatoriTransaction(formdata=formData)
    sendSatoriTransaction.address.data = ''
    sendSatoriTransaction.amount.data = ''
    return sendSatoriTransaction


@app.route('/vault/<network>', methods=['GET', 'POST'])
def vaultMainTest(network: str = 'main'):
    return vault()


def presentVaultPasswordForm():
    '''
    this function could be used to fill a form with the current
    configuration for a stream in order to edit it.
    '''
    passwordForm = forms.VaultPassword(formdata=request.form)
    passwordForm.password.data = ''
    return passwordForm


@app.route('/vault', methods=['GET', 'POST'])
def vault():

    def accept_submittion(passwordForm):
        _rvn, _evr = start.openVault(
            password=passwordForm.password.data,
            create=True)
        # if rvn is None or not rvn.isEncrypted:
        #    flash('unable to open vault')

    if request.method == 'POST':
        accept_submittion(forms.VaultPassword(formdata=request.form))
    if start.vault is not None and not start.vault.isEncrypted:
        start.vault.get(allWalletInfo=False)
        return render_template('vault.html', **getResp({
            'title': 'Vault',
            'walletIcon': 'lock',
            'image': getQRCode(start.vault.address),
            'network': 'test',  # change to main when ready
            'retain': (start.vault.getAutosecureEntry() or {}).get('retain', 0),
            'autosecured': start.vault.autosecured(),
            'vaultPasswordForm': presentVaultPasswordForm(),
            'vaultOpened': True,
            'wallet': start.vault,
            'sendSatoriTransaction': presentSendSatoriTransactionform(request.form)}))
    return render_template('vault.html', **getResp({
        'title': 'Vault',
        'walletIcon': 'lock',
        'image': '',
        'network': 'test',  # change to main when ready
        'autosecured': False,
        'vaultPasswordForm': presentVaultPasswordForm(),
        'vaultOpened': False,
        'wallet': start.vault,
        'sendSatoriTransaction': presentSendSatoriTransactionform(request.form)}))


@app.route('/enable_autosecure/<network>/<retainInWallet>', methods=['GET'])
def enableAutosecure(network: str = 'main', retainInWallet: int = 0):
    try:
        retainInWallet = int(retainInWallet)
    except Exception as _:
        retainInWallet = 0
    if start.vault is None:
        flash('Must unlock your vault to enable autosecure.')
        return redirect('/dashboard')
    # for this network open the wallet get the address
    # config.get('autosecure')
    # save the address to the autosecure config
    # as the value save the map:
    # {'address': vaultAddress, 'pubkey': vaultPubkey, 'sig': signature}
    # make the signature sign the encrypted string representation of their vault
    # plus the vaultAddress
    # that way we can verify the signature is for this vault in the future.
    # the config will be checked daily when value comes in.
    config.add(
        'autosecure',
        data={
            start.getWallet(network=network).address: {
                **start.vault.authPayload(
                    asDict=True,
                    challenge=start.vault.address + start.vault.publicKey),
                **{'retain': retainInWallet}
            }
        })
    # start.getWallet(network=network).get() # we think this triggers the tx twice
    return 'OK', 200


@app.route('/disable_autosecure/<network>', methods=['GET'])
def disableAutosecure(network: str = 'main'):
    # find the entry in the autosecure config of this wallet's nework address
    # remove it, save the config
    config.put(
        'autosecure',
        data={
            k: v for k, v in config.get('autosecure').items()
            if k != start.getWallet(network=network).address})
    return 'OK', 200


@app.route('/vote', methods=['GET', 'POST'])
def vote():

    def getVotes(wallet):

        def valuesAsNumbers(map: dict):
            return {k: int(v) for k, v in map.items()}

        x = {
            'communityVotes': start.server.getManifestVote(),
            'walletVotes': {k: v/100 for k, v in start.server.getManifestVote(wallet).items()},
            'vaultVotes': (
                valuesAsNumbers(
                    {k: v/100 for k, v in start.server.getManifestVote(start.vault).items()})
                if start.vault is not None and start.vault.isDecrypted else {
                    'predictors': 0,
                    'oracles': 0,
                    'creators': 0,
                    'managers': 0})}
        # logging.debug('x', x, color='yellow')
        return x

    def getStreams(wallet):
        # todo convert result to the strucutre the template expects:
        # [ {'cols': 'value'}]
        streams = start.server.getSanctionVote(wallet, start.vault)
        # logging.debug('streams', [
        #              s for s in streams if s['oracle_alias'] is not None], color='yellow')
        return streams
        # return []
        # return [{
        #    'sanctioned': 10,
        #    'active': True,
        #    'oracle_pubkey': 'pubkey',
        #    'oacle_alias': 'alias',
        #    'stream': 'stream',
        #    'target': 'target',
        #    'start': 'start',
        #    'cadence': 60*10,
        #    'id': '0',
        #    'total_vote': 27,
        # }, {
        #    'sanctioned': 0,
        #    'active': False,
        #    'oracle': 'pubkey',
        #    'alias': 'alias',
        #    'stream': 'stream',
        #    'target': 'target',
        #    'start': 'start',
        #    'cadence': 60*15,
        #    'id': '1',
        #    'vote': 36,
        # }]

    def accept_submittion(passwordForm):
        _rvn, _evr = start.openVault(password=passwordForm.password.data)
        # if rvn is None and not rvn.isEncrypted:
        #    flash('unable to open vault')

    if request.method == 'POST':
        accept_submittion(forms.VaultPassword(formdata=request.form))

    myWallet = start.getWallet(network='test')
    if start.vault is not None and not start.vault.isEncrypted:
        return render_template('vote.html', **getResp({
            'title': 'Vote',
            'network': 'test',  # change to main when ready
            'vaultPasswordForm': presentVaultPasswordForm(),
            'vaultOpened': True,
            'wallet': myWallet,
            'vault': start.vault,
            'streams': getStreams(myWallet),
            **getVotes(myWallet)}))
    return render_template('vote.html', **getResp({
        'title': 'Vote',
        'network': 'test',  # change to main when ready
        'vaultPasswordForm': presentVaultPasswordForm(),
        'vaultOpened': False,
        'wallet': myWallet,
        'vault': start.vault,
        'streams': getStreams(myWallet),
        **getVotes(myWallet)}))


@app.route('/vote/submit/manifest/wallet', methods=['POST'])
def voteSubmitManifestWallet():
    # logging.debug(request.json, color='yellow')
    if (
        request.json.get('walletPredictors') > 0 or
        request.json.get('walletOracles') > 0 or
        request.json.get('walletCreators') > 0 or
        request.json.get('walletManagers') > 0
    ):
        start.server.submitMaifestVote(
            wallet=start.getWallet(network='test'),
            votes={
                'predictors': request.json.get('walletPredictors', 0),
                'oracles': request.json.get('walletOracles', 0),
                'creators': request.json.get('walletCreators', 0),
                'managers': request.json.get('walletManagers', 0)})
    return jsonify({'message': 'Manifest votes received successfully'}), 200


@app.route('/vote/submit/manifest/vault', methods=['POST'])
def voteSubmitManifestVault():
    # logging.debug(request.json, color='yellow')
    if ((
            request.json.get('vaultPredictors') > 0 or
            request.json.get('vaultOracles') > 0 or
            request.json.get('vaultCreators') > 0 or
            request.json.get('vaultManagers') > 0) and
            start.vault is not None and start.vault.isDecrypted
        ):
        start.server.submitMaifestVote(
            start.vault,
            votes={
                'predictors': request.json.get('vaultdictors', 0),
                'oracles': request.json.get('vaultOracles', 0),
                'creators': request.json.get('vaultreators', 0),
                'managers': request.json.get('vaultanagers', 0)})
    return jsonify({'message': 'Manifest votes received successfully'}), 200


@app.route('/vote/submit/sanction/wallet', methods=['POST'])
def voteSubmitSanctionWallet():
    # logging.debug(request.json, color='yellow')
    # {'walletStreamIds': [0], 'vaultStreamIds': [], 'walletVotes': [27], 'vaultVotes': []}
    # zip(walletStreamIds, walletVotes)
    # {'walletStreamIds': [None], 'walletVotes': [1]}
    if (
        len(request.json.get('walletStreamIds', [])) > 0 and
        len(request.json.get('walletVotes', [])) > 0 and
        len(request.json.get('walletStreamIds', [])) == len(request.json.get(
            'walletVotes', []))
    ):
        start.server.submitSanctionVote(
            wallet=start.getWallet(network='test'),
            votes={
                'streamIds': request.json.get('walletStreamIds'),
                'votes': request.json.get('walletVotes')})
    return jsonify({'message': 'Stream votes received successfully'}), 200


@app.route('/vote/submit/sanction/vault', methods=['POST'])
def voteSubmitSanctionVault():
    # logging.debug(request.json, color='yellow')
    # {'walletStreamIds': [0], 'vaultStreamIds': [], 'walletVotes': [27], 'vaultVotes': []}
    # zip(walletStreamIds, walletVotes)
    if (
        len(request.json.get('vaultStreamIds', [])) > 0 and
        len(request.json.get('vaultVotes', [])) > 0 and
        len(request.json.get('vaultStreamIds')) == len(request.json.get('vaultVotes', [])) and
        start.vault is not None and start.vault.isDecrypted
    ):
        start.server.submitSanctionVote(
            start.vault,
            votes={
                'streamIds': request.json.get('vaultStreamIds'),
                'votes': request.json.get('vaultVotes')})
    return jsonify({'message': 'Stream votes received successfully'}), 200


# todo: this needs a ui button.
# this ability to clear them all lets us just display a subset of streams to vote on with a search for a specific one


@app.route('/vote/remove_all/sanction', methods=['GET'])
def voteRemoveAllSanction():
    # logging.debug(request.json, color='yellow')
    start.server.removeSanctionVote(wallet=start.getWallet(network='test'))
    if (start.vault is not None and start.vault.isDecrypted):
        start.server.removeSanctionVote(start.vaul)
    return jsonify({'message': 'Stream votes received successfully'}), 200


@app.route('/relay_csv', methods=['GET'])
def relayCsv():
    ''' returns a csv file of the current relay streams '''
    import pandas as pd
    return (
        pd.DataFrame([{
            **{'source': stream.streamId.source},
            **{'author': stream.streamId.author},
            **{'stream': stream.streamId.stream},
            **{'target': stream.streamId.target},
            **stream.asMap(noneToBlank=True),
            **{'latest': start.relay.latest.get(stream.streamId.topic(), '')},
            **{'cadenceStr': deduceCadenceString(stream.cadence)},
            **{'offsetStr': deduceOffsetString(stream.offset)}}
            for stream in start.relay.streams]
            if start.relay is not None else []).to_csv(index=False),
        200,
        {
            'Content-Type': 'text/csv',
            'Content-Disposition': 'attachment; filename=relay_streams.csv'
        }
    )


@app.route('/relay_history_csv/<topic>', methods=['GET'])
def relayHistoryCsv(topic: str = None):
    ''' returns a csv file of the history of the relay stream '''
    cache = start.cacheOf(StreamId.fromTopic(topic))
    return (
        (
            cache.df.drop(columns=['hash'])
            if cache is not None and cache.df is not None and 'hash' in cache.df.columns
            else pd.DataFrame({'failure': [
                f'no history found for stream with stream id of {topic}']}
            )
        ).to_csv(),
        200,
        {
            'Content-Type': 'text/csv',
            'Content-Disposition': f'attachment; filename={cache.id.stream}.{cache.id.target}.csv'
        })


@app.route('/merge_history_csv/<topic>', methods=['POST'])
def mergeHistoryCsv(topic: str = None):
    ''' merge history uploaded  '''
    cache = start.cacheOf(StreamId.fromTopic(topic))
    if cache is not None:
        msg, status, f = getFile('.csv')
        if f is not None:
            df = pd.read_csv(f)
            cache.merge(df)
            success, _ = cache.validateAllHashes()
            if success:
                flash('history merged successfully!', 'success')
            else:
                cache.saveHashes()
                success, _ = cache.validateAllHashes()
                if success:
                    flash('history merged successfully!', 'success')
        else:
            flash(msg, 'success' if status == 200 else 'error')
    else:
        flash('history data not found', 'error')
    return redirect(url_for('dashboard'))


@app.route('/remove_history_csv/<topic>', methods=['GET'])
def removeHistoryCsv(topic: str = None):
    ''' removes history '''
    cache = start.cacheOf(StreamId.fromTopic(topic))
    if cache is not None and cache.df is not None:
        cache.remove()
        flash('history cleared successfully', 'success')
    else:
        flash('history not found', 'error')
    return redirect(url_for('dashboard'))


@app.route('/trigger_relay/<topic>', methods=['GET'])
def triggerRelay(topic: str = None):
    ''' triggers relay stream to happen '''
    if start.relay.triggerManually(StreamId.fromTopic(topic)):
        flash('relayed successfully', 'success')
    else:
        flash('failed to relay', 'error')
    return redirect(url_for('dashboard'))

###############################################################################
## Routes - subscription ######################################################
###############################################################################

# unused - we're not using any other networks yet, but when we do we can pass
# their values to this and have it diseminate
# @app.route('/subscription/update/', methods=['POST'])
# def update():
#    """
#    returns nothing
#    ---
#    post:
#      operationId: score
#      requestBody:
#        content:
#          application/json:
#            {
#            "source-id": id,
#            "stream-id": id,
#            "observation-id": id,
#            "content": {
#                key: value
#            }}
#      responses:
#        '200':
#          json
#    """
#    ''' from streamr - datastream has a new observation
#    upon a new observation of a datastream, the nodejs app will send this
#    python flask app a message on this route. The flask app will then pass the
#    message to the data manager, specifically the scholar (and subscriber)
#    threads by adding it to the appropriate subject. (the scholar, will add it
#    to the correct table in the database history, notifying the subscriber who
#    will, if used by any current best models, notify that model's predictor
#    thread via a subject that a new observation is available by providing the
#    observation directly in the subject).
#
#    This app needs to create the DataManager, ModelManagers, and Learner in
#    in order to have access to those objects. Specifically the DataManager,
#    we need to be able to access it's BehaviorSubjects at data.newData
#    so we can call .on_next() here to pass along the update got here from the
#    Streamr LightClient, and trigger a new prediction.
#    '''
#    x = Observation.parse(request.json)
#    start.engine.data.newData.on_next(x)
#
#    return request.json

###############################################################################
## Routes - history ###########################################################
# we may be able to make these requests
###############################################################################


@app.route('/history/request')
def publsih():
    ''' to streamr - create a new datastream to publish to '''
    # todo: spoof a dataset response - random generated data, so that the
    #       scholar can be built to ask for history and download it.
    return render_template('unknown.html', **getResp())


@app.route('/history')
def publsihMeta():
    ''' to streamr - publish to a stream '''
    return render_template('unknown.html', **getResp())

###############################################################################
## UDP communication ##########################################################
###############################################################################


@app.route('/synapse/ping', methods=['GET'])
def synapsePing():
    ''' tells p2p script we're up and running '''
    if start.wallet is None:
        return 'fail', 400
    if start.synergy is not None:
        return 'ready', 200
    return 'ok', 200


@app.route('/synapse/ports', methods=['GET'])
def synapsePorts():
    ''' receives data from udp relay '''
    return str(start.peer.gatherChannels())


@app.route('/synapse/stream')
def synapseStream():
    ''' here we listen for messages from the synergy engine '''

    def event_stream():
        while True:
            message = start.udpQueue.get()
            if isinstance(message, Envelope):
                yield 'data:' + message.toJson + '\n\n'

    return Response(
        stream_with_context(event_stream()),
        content_type='text/event-stream')


@app.route('/synapse/message', methods=['POST'])
def synapseMessage():
    ''' receives data from udp relay '''
    data = request.data
    remoteIp = request.headers.get('remoteIp')
    # remotePort = int(request.headers.get('remotePort')) #not needed at this time
    # localPort = int(request.headers.get('localPort'))
    if any(v is None for v in [remoteIp, data]):
        return 'fail', 400
    start.synergy.passMessage(remoteIp, message=data)
    return 'ok', 200


###############################################################################
## Entry ######################################################################
###############################################################################


if __name__ == '__main__':
    # if False:
    #    spoofStreamer()

    # serve(app, host='0.0.0.0', port=config.get()['port'])
    if not debug:
        webbrowser.open('http://127.0.0.1:24601', new=0, autoraise=True)
    app.run(
        host='0.0.0.0',
        port=config.flaskPort(),
        threaded=True,
        debug=debug,
        use_reloader=False)   # fixes run twice issue
    # app.run(host='0.0.0.0', port=config.get()['port'], threaded=True)
    # https://stackoverflow.com/questions/11150343/slow-requests-on-local-flask-server
    # did not help

# http://localhost:24601/
# sudo nohup /app/anaconda3/bin/python app.py > /dev/null 2>&1 &
# > python satori\web\app.py
