"""Use Colab CLI's documented DriveFS hook only after explicit mount consent."""
import json
import time


def install_mount_hook(runtime, client, endpoint, emit, directory):
    state_file = directory / 'drive-native-auth.json'

    def decode(response):
        if response.status_code != 200:
            raise RuntimeError('Colab Drive 授权接口失败（HTTP %d）' % response.status_code)
        return json.loads(response.text.split('\n', 1)[-1])

    def hook(message, wsclient):
        if message.get('content', {}).get('request', {}).get('authType') != 'dfs_ephemeral':
            return False
        msg_id = message.get('metadata', {}).get('colab_msg_id')
        url = client.colab_domain + '/tun/m/credentials-propagation/' + endpoint
        params = dict(authuser='0', authtype='dfs_ephemeral', version='2', dryrun='true', propagate='true', record='false')
        token = decode(client.session.request('GET', url, params=params)).get('token')
        headers = {'x-goog-colab-token': token}
        def post():
            return decode(client.session.request('POST', url, params=params,
                headers=headers, files={'file_id': (None, 'empty.ipynb')}))
        try:
            prior = json.loads(state_file.read_text())
            previously_confirmed = prior.get('endpoint') == endpoint and prior.get('confirmed') is True
        except (OSError, ValueError):
            prior = {}
            previously_confirmed = False
        # Keep the existing callback for this same VM until the user finishes.
        # Repeated dry-run POSTs create new callback tokens without propagating
        # the completed consent. A local acknowledgement triggers propagation.
        if prior.get('endpoint') == endpoint and prior.get('pending') and prior.get('url'):
            data = {'success': False, 'unauthorized_redirect_uri': prior['url']}
        else:
            data = post()
        if not data.get('success') and not previously_confirmed:
            uri = data.get('unauthorized_redirect_uri')
            if not uri:
                raise RuntimeError('Colab 未返回 Drive 授权入口')
            state_file.write_text(json.dumps({'url': uri, 'pending': True, 'endpoint': endpoint}))
            emit('prepare', '需要在工作台“账号与连接”打开 Colab Drive 授权链接，确认后自动继续')
            for _ in range(120):
                time.sleep(5)
                try:
                    if json.loads(state_file.read_text()).get('confirmed'):
                        break
                except (OSError, ValueError):
                    pass
            else:
                raise RuntimeError('等待 Colab Drive 授权超时')
        params['dryrun'] = 'false'
        data = post()
        if data.get('success') is False:
            raise RuntimeError('Colab 没有确认 Drive 凭据传播完成')
        state_file.write_text(json.dumps({'pending': False}))
        emit('prepare', 'Colab 已确认当前运行时的 Drive 访问')
        reply = wsclient.session.msg('input_reply', {'value': {'type': 'colab_reply', 'colab_msg_id': msg_id}})
        if 'header' in message:
            reply['parent_header'] = message['header']
        wsclient.stdin_channel.send(reply)
        return True
    def guarded_hook(message, wsclient):
        try:
            return hook(message, wsclient)
        except Exception:
            if message.get('content', {}).get('request', {}).get('authType') != 'dfs_ephemeral':
                raise
            # ColabRuntime swallows hook exceptions. Return a protocol error so
            # the remote mount exits and the owner can perform cleanup.
            state_file.write_text(json.dumps({'pending':False}))
            error = 'Drive 接入未完成，本次缓存操作已停止'
            emit('prepare', error)
            value = {'type':'colab_reply','colab_msg_id':message.get('metadata',{}).get('colab_msg_id'),'error':error}
            reply = wsclient.session.msg('input_reply', {'value':value})
            if 'header' in message:
                reply['parent_header'] = message['header']
            wsclient.stdin_channel.send(reply)
            return True
    runtime.colab_request_hook = guarded_hook
