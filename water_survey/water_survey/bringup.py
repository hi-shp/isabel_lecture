"""Control only the dedicated base service; never accept shell commands from HTTP."""
import asyncio


class BringupManager:
    unit = 'water-survey-base.service'

    def __init__(self, enabled=False):
        self.enabled = enabled
        self.state = dict(available=enabled, state='unknown', detail='상태 확인 중')
        self.lock = asyncio.Lock()

    async def refresh(self):
        if not self.enabled:
            self.state = dict(available=False, state='unmanaged', detail='외부에서 실행 중인 브링업을 사용합니다.')
            return
        try:
            code, text = await self.run('/usr/bin/systemctl', 'show', self.unit,
                                        '--property=ActiveState,SubState,LoadState')
            values = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
            loaded = values.get('LoadState') == 'loaded'
            self.state = dict(available=loaded, state=values.get('ActiveState', 'unknown'),
                              detail=values.get('SubState', text.strip()) if loaded else '브링업 서비스 설치 필요')
        except (OSError, TimeoutError) as exc:
            self.state = dict(available=False, state='unknown', detail=str(exc)[:150])

    async def command(self, action):
        if not self.enabled or action not in ('start', 'stop'):
            raise ValueError('브링업 제어가 설정되지 않았습니다.')
        async with self.lock:
            code, text = await self.run('/usr/bin/sudo', '-n', '/usr/bin/systemctl',
                                        '--no-block', action, self.unit)
            if code:
                raise ValueError('브링업 요청 실패: ' + text.strip()[:200])
            self.state.update(state='activating' if action == 'start' else 'deactivating', detail='요청 처리 중')

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
