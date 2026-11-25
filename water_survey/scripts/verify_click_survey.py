#!/usr/bin/env python3
"""Four-cell click-position test with a continuous WebSocket reader.

Requires fresh DO, refuses an active survey, never arms propulsion, and stops
only the mission created by this check if it fails. Previous records remain.
"""
import argparse
import asyncio
import contextlib
import json
from pathlib import Path
import time
import aiohttp


class DashboardSession:
    def __init__(self, http, base):
        self.http, self.base = http, base.rstrip('/')
        self.ws = self.client = self.reader = self.heartbeat = None
        self.pongs = asyncio.Queue()
        self.revision = 0

    async def connect(self):
        self.hello = asyncio.get_running_loop().create_future()
        self.ws = await self.http.ws_connect(self.base+'/ws', headers={'Origin':self.base}, heartbeat=2)
        self.reader = asyncio.create_task(self.consume())
        self.client = await asyncio.wait_for(self.hello, 5)
        self.heartbeat = asyncio.create_task(self.keepalive())
        return self

    async def consume(self):
        try:
            async for message in self.ws:
                if message.type != aiohttp.WSMsgType.TEXT:continue
                data = json.loads(message.data)
                if data.get('type') == 'hello' and not self.hello.done():
                    self.revision=data.get('control_revision',0);self.hello.set_result(data['client'])
                elif data.get('type') == 'pong':await self.pongs.put(data['sent'])
        finally:
            if not self.hello.done():self.hello.set_exception(ConnectionError('WebSocket closed before hello'))

    async def keepalive(self):
        while not self.ws.closed:
            await self.ws.send_str('heartbeat')
            await asyncio.sleep(1)

    async def post(self, action, data=None):
        async with self.http.post(self.base+'/api/'+action, json={'client':self.client,'control_revision':self.revision, **(data or {})},
                                  headers={'Origin':self.base}) as response:
            result = await response.json()
            if response.status != 200:raise RuntimeError(f'{action}: {response.status}: {result}')
            self.revision=result.get('control_revision',self.revision)
            return result

    async def ping_ms(self):
        sent = time.monotonic()
        await self.ws.send_json({'type':'ping', 'sent':sent})
        async with asyncio.timeout(5):
            while await self.pongs.get() != sent:pass
        return (time.monotonic()-sent)*1000

    async def close(self):
        if self.heartbeat:
            self.heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):await self.heartbeat
        if self.ws:await self.ws.close()
        if self.reader:await self.reader
        self.client = None


async def verify(base, output):
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    session = mission = None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
        async def state():
            async with http.get(base+'/api/state') as response:
                response.raise_for_status()
                return await response.json()
        before = await state()
        if before['state'] in ('running','dwelling'):raise RuntimeError('Existing survey is active; left untouched')
        if before.get('do_age_s') is None or before['do_age_s']>5:raise RuntimeError('Fresh DO required; no mission started')
        published = before['hardware_publish_count']
        (output/'before.json').write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding='utf-8')
        try:
            session = await DashboardSession(http, base).connect()
            area = {'south':35.1777,'west':129.1198,'north':35.177735,'east':129.119842}
            value = await session.post('configure', {'mode':'simulated','pattern':'coverage','route':[],
                'area':area,'settings':{'cell_m':2,'dwell_s':3,'radius_m':1,'throttle':10}})
            assert value['coverage']['total']==4, value['coverage']
            route = value['route']
            await session.post('virtual', route[0]); value = await session.post('start')
            mission, count = value['mission_id'], value['count']
            await session.close(); session = None
            await asyncio.sleep(1.3)
            value = await state()
            assert value['mission_id']==mission and value['hardware_publish_count']==published, value
            assert value['count']>count, 'No DO received during disconnect'
            session = await DashboardSession(http, base).connect(); rtt = await session.ping_ms()
            for index, target in enumerate(route):
                await session.post('virtual', target); deadline = time.monotonic()+12
                while time.monotonic()<deadline:
                    value = await state()
                    if value['mission_id']!=mission:raise RuntimeError('Survey changed externally; left untouched')
                    if value['state']=='complete' or value['waypoint']>index:break
                    if value['state']=='stopped':raise RuntimeError(value['reason'])
                    await asyncio.sleep(.2)
                else:raise RuntimeError('Waypoint did not advance: '+str(value['reason']))
            assert value['state']=='complete' and value['coverage']=={'total':4,'observed':4}, value
            assert value['hardware_publish_count']==published, value
            async with http.get(base+'/api/export/'+mission) as response:
                response.raise_for_status(); (output/'rectangle-click-survey.zip').write_bytes(await response.read())
            summary = {key:value[key] for key in ('mission_id','state','count','mapped','coverage',
                       'color_scale','do_range','hardware_publish_count','bringup')}
            summary.update(app_rtt_ms=round(rtt,2), hardware_publish_delta=value['hardware_publish_count']-published)
            (output/'rectangle-click-verification.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
            return summary
        finally:
            try:
                latest = await state()
                if (mission and latest.get('mission_id')==mission and latest.get('mode')=='simulated'
                    and latest['state'] in ('running','dwelling')):
                    if session is None or session.ws.closed:
                        if session is not None:await session.close()
                        session = await DashboardSession(http, base).connect()
                    await session.post('stop')
            finally:
                if session is not None:await session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='http://localhost:8787')
    parser.add_argument('--output', default='survey-verification')
    args = parser.parse_args()
    print(json.dumps(asyncio.run(verify(args.base.rstrip('/'), args.output)), ensure_ascii=False, indent=2))


if __name__ == '__main__':main()
