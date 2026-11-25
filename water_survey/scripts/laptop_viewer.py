#!/usr/bin/env python3
"""Local-only dashboard proxy: localhost is a secure context for browser capture.

Usage: python laptop_viewer.py --ship http://192.168.80.78:8000
The vessel mission remains independent of this laptop process.
"""
import argparse
import asyncio
import contextlib
import os
from pathlib import Path
import shutil
import subprocess
import webbrowser
from urllib.parse import urlsplit
from aiohttp import web, ClientSession, ClientTimeout, WSMsgType


def browser_command(url, fullscreen=True, browser=None):
    """Use an existing Chromium app window, without changing browser settings."""
    candidates=[browser] if browser else []
    if os.name == 'nt':
        candidates.extend(str(Path(os.environ.get(name,default))/suffix) for name,default,suffix in (
            ('PROGRAMFILES','C:/Program Files','Google/Chrome/Application/chrome.exe'),
            ('LOCALAPPDATA',str(Path.home()/'AppData/Local'),'Google/Chrome/Application/chrome.exe'),
            ('PROGRAMFILES(X86)','C:/Program Files (x86)','Microsoft/Edge/Application/msedge.exe'),
            ('PROGRAMFILES','C:/Program Files','Microsoft/Edge/Application/msedge.exe')))
    candidates.extend(shutil.which(name) for name in ('google-chrome','chromium','microsoft-edge'))
    executable=next((str(p) for p in candidates if p and Path(p).is_file()),None)
    if not executable:return None
    return [executable,'--new-window','--app='+url,*(['--start-fullscreen'] if fullscreen else [])]


def open_dashboard(url, fullscreen=True, browser=None):
    command=browser_command(url,fullscreen,browser)
    if command:
        return subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    webbrowser.open(url,new=1)
    print('Chrome/Edge not found: opened the default browser. Use F11 for fullscreen.',flush=True)
    return None


def app_for(ship):
    app = web.Application(client_max_size=200_000)

    async def startup(app):
        app['session'] = ClientSession(timeout=ClientTimeout(total=60))

    async def cleanup(app):
        await app['session'].close()

    async def proxy(request):
        if request.method == 'POST' or request.path == '/ws':
            if request.headers.get('Origin') != 'http://'+request.host:
                return web.Response(status=403, text='Open this dashboard on localhost.')
        headers = {'Origin': ship, 'Host': urlsplit(ship).netloc}
        if request.content_type:
            headers['Content-Type'] = request.headers.get('Content-Type', 'application/octet-stream')
        target = ship+str(request.rel_url)
        try:
            if request.path == '/ws':
                async with app['session'].ws_connect(target, headers=headers, heartbeat=5) as upstream:
                    local = web.WebSocketResponse(heartbeat=5)
                    await local.prepare(request)

                    async def relay(source, destination):
                        async for message in source:
                            if message.type == WSMsgType.TEXT:
                                await destination.send_str(message.data)
                            elif message.type == WSMsgType.BINARY:
                                await destination.send_bytes(message.data)
                    tasks = [asyncio.create_task(relay(local, upstream)), asyncio.create_task(relay(upstream, local))]
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await local.close()
                    return local
            async with app['session'].request(request.method, target, data=await request.read(),
                                               headers=headers, allow_redirects=False) as response:
                result_headers = {k: v for k, v in response.headers.items() if k.lower() in
                                  ('content-type', 'content-disposition', 'cache-control', 'etag')}
                return web.Response(status=response.status, body=await response.read(), headers=result_headers)
        except (OSError, asyncio.TimeoutError) as exc:
            return web.json_response({'error': '선박 연결 대기: '+str(exc)}, status=502)

    app.router.add_route('*', '/{path:.*}', proxy)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ship', default='http://192.168.80.78:8000')
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--windowed', action='store_true', help='Open a separate app window without fullscreen')
    parser.add_argument('--browser', help='Path to an existing Chrome/Edge executable')
    args = parser.parse_args()
    parsed = urlsplit(args.ship)
    if parsed.scheme != 'http' or not parsed.hostname or parsed.path not in ('', '/') or parsed.username or parsed.password:
        parser.error('ship must be an http://hostname:port origin')
    app=app_for(args.ship.rstrip('/'))
    if not args.no_browser:
        async def open_window(app):
            async def ready():
                for _ in range(100):
                    try:
                        _,writer=await asyncio.wait_for(asyncio.open_connection('127.0.0.1',args.port),.2)
                        writer.close();await writer.wait_closed()
                        await asyncio.to_thread(open_dashboard,f'http://localhost:{args.port}',not args.windowed,args.browser)
                        return
                    except OSError:await asyncio.sleep(.05)
                print('Local proxy not ready; dashboard window was not opened.',flush=True)
            app['window_task']=asyncio.create_task(ready())
        async def stop_window_task(app):
            app['window_task'].cancel()
            with contextlib.suppress(asyncio.CancelledError):await app['window_task']
        app.on_startup.append(open_window)
        app.on_cleanup.append(stop_window_task)
    web.run_app(app, host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
