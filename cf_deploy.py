# -*- coding: utf-8 -*-
r"""
Cloudflare Worker API 部署脚本（Python，无 Node 依赖）
用法:
  python cf_deploy.py <API_TOKEN> [--script NAME] [--dry-run]
  或设置环境变量 CF_API_TOKEN

流程:
  1. 列出账号 (account_id)
  2. 通过自定义域 zzzj.de5.net 找到对应的 worker 脚本名
  3. 备份当前线上脚本到 桌面\worker-backup-<时间>.js
  4. 快照当前 settings (compatibility_date / flags / bindings)
  5. 上传桌面 worker.js (module 格式, keep_bindings=true)
  6. 探测线上验证 v5 生效
"""
import sys, os, json, time, urllib.request, urllib.error, ssl

API = 'https://api.cloudflare.com/client/v4'
WORKER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'worker.js')
BACKUP_DIR = r'C:\Users\Administrator\Desktop'
DOMAIN_HINT = 'zzzj.de5.net'

def call(token, method, path, body=None, content_type='application/json', raw=False):
    url = API + path
    data = None
    headers = {'Authorization': 'Bearer ' + token}
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            headers['Content-Type'] = content_type
        elif isinstance(body, bytes):
            data = body
            headers['Content-Type'] = content_type
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        r = urllib.request.urlopen(req, timeout=60)
        payload = r.read()
        status = r.status
    except urllib.error.HTTPError as e:
        payload = e.read()
        status = e.code
    if raw:
        return status, payload
    try:
        j = json.loads(payload.decode('utf-8', 'replace'))
    except Exception:
        return status, {'raw': payload[:500].decode('utf-8', 'replace')}
    return status, j

def die(msg):
    print('!! ' + msg)
    sys.exit(1)

def main():
    args = sys.argv[1:]
    dry = '--dry-run' in args
    script_name = None
    if '--script' in args:
        script_name = args[args.index('--script') + 1]
    token = None
    if len(args) > 0 and not args[0].startswith('--'):
        token = args[0]
    token = token or os.environ.get('CF_API_TOKEN')
    if not token:
        die('缺少 API Token: python cf_deploy.py <TOKEN>')

    # 1. 账号
    st, j = call(token, 'GET', '/accounts')
    if st != 200 or not j.get('success'):
        die('列账号失败 (HTTP %d): %s' % (st, json.dumps(j)[:300]))
    accounts = j.get('result', [])
    if not accounts:
        die('Token 可见的账号为空')
    print('== 账号 ==')
    for a in accounts:
        print('  %s | %s' % (a['id'], a['name']))
    account_id = accounts[0]['id']
    if len(accounts) > 1:
        print('  (多账号，默认用第一个，可用 --account 指定)')

    # 2. 找脚本名
    if not script_name:
        st, j = call(token, 'GET', '/accounts/%s/workers/domains' % account_id)
        domains = j.get('result', []) if st == 200 else []
        for d in domains:
            host = (d.get('hostname') or '')
            if DOMAIN_HINT in host:
                script_name = d.get('service')
                print('== 域名匹配 ==\n  %s -> 脚本 "%s" (env: %s)' % (host, script_name, d.get('environment')))
                break
        if not script_name:
            st, j = call(token, 'GET', '/accounts/%s/workers/scripts' % account_id)
            scripts = [s['id'] for s in (j.get('result') or []) if isinstance(s, dict)]
            print('== 域名未匹配到，账号下脚本列表 ==\n  ' + ', '.join(scripts))
            die('请用 --script <名称> 指定目标脚本')

    # 3. 备份当前线上脚本
    st, backup = call(token, 'GET', '/accounts/%s/workers/scripts/%s' % (account_id, script_name), raw=True)
    if st == 200 and len(backup) > 1000:
        bpath = os.path.join(BACKUP_DIR, 'worker-backup-%s.js' % time.strftime('%H%M%S'))
        with open(bpath, 'wb') as f:
            f.write(backup)
        print('== 已备份线上脚本 ==\n  %s (%d B)' % (bpath, len(backup)))
    else:
        print('== 警告: 备份失败 (HTTP %d, %d B)，继续部署' % (st, len(backup) if isinstance(backup, bytes) else -1))

    # 4. 快照 settings
    st, j = call(token, 'GET', '/accounts/%s/workers/scripts/%s/settings' % (account_id, script_name))
    compat_date = '2026-03-13'
    compat_flags = ['enable_request_signal']
    bindings = []
    if st == 200 and j.get('success'):
        r = j.get('result', {}) or {}
        compat_date = r.get('compatibility_date') or compat_date
        compat_flags = r.get('compatibility_flags') or compat_flags
        bindings = r.get('bindings') or []
        print('== 当前 settings ==\n  compat_date=%s flags=%s' % (compat_date, compat_flags))
        print('  bindings: ' + ', '.join('%s(%s)' % (b.get('name'), b.get('type')) for b in bindings))
    else:
        print('== 警告: 读取 settings 失败，使用默认 compat %s' % compat_date)

    if dry:
        print('== DRY-RUN 结束（未上传）==')
        return

    # 5. 上传 (module multipart, keep_bindings)
    src = open(WORKER_FILE, 'rb').read()
    keep_types = sorted(set(b.get('type') for b in bindings if b.get('type') and b.get('type') != 'assets')) or None
    metadata = {
        'main_module': 'worker.js',
        'compatibility_date': compat_date,
        'compatibility_flags': compat_flags,
        'keep_bindings': keep_types,
    }
    boundary = '----cfdeploy' + str(int(time.time()))
    parts = []
    parts.append(('--%s\r\nContent-Disposition: form-data; name="metadata"\r\nContent-Type: application/json\r\n\r\n%s\r\n' % (boundary, json.dumps(metadata))).encode())
    parts.append(('--%s\r\nContent-Disposition: form-data; name="worker.js"; filename="worker.js"\r\nContent-Type: application/javascript+module\r\n\r\n' % boundary).encode() + src + b'\r\n')
    parts.append(('--%s--\r\n' % boundary).encode())
    body = b''.join(parts)
    st, j = call(token, 'PUT', '/accounts/%s/workers/scripts/%s' % (account_id, script_name), body, 'multipart/form-data; boundary=' + boundary)
    print('== 上传结果 ==')
    print('  HTTP %d | success=%s' % (st, j.get('success')))
    if st != 200 or not j.get('success'):
        print('  ' + json.dumps(j, ensure_ascii=False)[:800])
        die('上传失败 —— 线上仍是旧版本，可用桌面备份回滚')
    print('  部署成功: %s (%d B)' % (script_name, len(src)))

    # 6. 验证
    print('== 8 秒后探测线上 ==')
    time.sleep(8)
    vpath = '/Items/vb-1034/Images/Primary?tag=ZTRhMmE3LnBuZw'
    req = urllib.request.Request('https://zzzj.de5.net' + vpath, headers={'User-Agent': 'Infuse-Direct/8.5.2'})
    class NR(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a):
            return None
    try:
        r = urllib.request.build_opener(NR()).open(req, timeout=45)
        data = r.read()
        print('  vb-1034: HTTP %d, %d B, cover=%s' % (r.status, len(data), r.headers.get('x-emby-proxy-cover')))
    except urllib.error.HTTPError as e:
        print('  vb-1034: HTTP %d, cover=%s diag=%s' % (e.code, e.headers.get('x-emby-proxy-cover'), (e.headers.get('x-emby-proxy-cover-diag') or '')[:120]))
    except Exception as e:
        print('  vb-1034: ERR ' + str(e)[:80])

if __name__ == '__main__':
    main()
