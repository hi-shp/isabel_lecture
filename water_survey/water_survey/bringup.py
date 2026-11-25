"""Control only the dedicated base service; never accept shell commands from HTTP."""
import asyncio
import time


class BringupManager:
    unit = 'water-survey-base.service'

    def __init__(self, enabled=False):
        self.enabled = enabled
        self.state = dict(available=enabled, state='unknown', detail='상태 확인 중')
        self.lock = asyncio.Lock()
        self.main_pid=0

    async def refresh(self):
        if not self.enabled:
            self.state = dict(available=False, state='unmanaged', detail='외부에서 실행 중인 브링업을 사용합니다.')
            return
        try:
            code, text = await self.run('/usr/bin/systemctl', 'show', self.unit,
                                        '--property=ActiveState,SubState,LoadState,MainPID')
            values = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
            loaded = values.get('LoadState') == 'loaded'
            self.main_pid=int(values.get('MainPID','0'))
            self.state = dict(available=loaded, state=values.get('ActiveState', 'unknown'),
                              detail=values.get('SubState', text.strip()) if loaded else '브링업 서비스 설치 필요')
        except (OSError, TimeoutError) as exc:
            self.state = dict(available=False, state='unknown', detail=str(exc)[:150])

    async def command(self, action):
        if not self.enabled or action not in ('start', 'stop','restart'):
            raise ValueError('브링업 제어가 설정되지 않았습니다.')
        async with self.lock:
            old_pid=self.main_pid
            code, text = await self.run('/usr/bin/sudo', '-n', '/usr/bin/systemctl',
                                        '--no-block', action, self.unit)
            if code:
                raise ValueError('브링업 요청 실패: ' + text.strip()[:200])
            self.state.update(state='deactivating' if action=='stop' else 'activating',detail='기존 구동 정리 후 새로 시작 중' if action=='restart' else '요청 처리 중')
            deadline=time.monotonic()+32
            while time.monotonic()<deadline:
                await asyncio.sleep(.4);await self.refresh()
                status=self.state['state']
                if status=='failed':raise ValueError('브링업 실행 실패 · 선박 로그를 확인하세요.')
                if action=='stop' and status=='inactive':return
                if action!='stop' and status=='active' and self.main_pid and (action!='restart' or self.main_pid!=old_pid):return
            raise ValueError('브링업 전환이 지연되고 있습니다. 현재 상태를 확인하세요.')

    @staticmethod
    async def run(*args):
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.STDOUT)
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), 8)
        except (asyncio.CancelledError, TimeoutError):
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            raise
        return proc.returncode, output.decode(errors='replace')
