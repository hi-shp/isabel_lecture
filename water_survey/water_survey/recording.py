import csv
import html
import io
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid
import zipfile
from .core import utc, latlon, xy

FIELDS = ['sequence', 'request_utc', 'received_utc', 'do_mg_l', 'lat', 'lon', 'position_source',
          'do_source', 'gps_valid', 'gps_age_s', 'gps_stamp', 'heading_deg', 'waypoint', 'phase',
          'response_ms', 'sensor', 'firmware', 'payload_hex', 'cell_x', 'cell_y']


def message_data(value):
    """JSON representation of all ROS fields, preserving nonfinite ray values."""
    if hasattr(value,'get_fields_and_field_types'):
        return {name:message_data(getattr(value,name)) for name in value.get_fields_and_field_types()}
    if isinstance(value,dict):return {key:message_data(v) for key,v in value.items()}
    if isinstance(value,float) and not math.isfinite(value):
        return 'NaN' if math.isnan(value) else ('+Inf' if value>0 else '-Inf')
    if value is None or isinstance(value,(str,bool,int,float)):return value
    return [message_data(v) for v in value]


def color(value, low, high):
    t = max(0, min(1, (value-low)/(high-low))) if high > low else .5
    stops = [(190, 58, 60), (235, 179, 64), (49, 147, 140), (42, 83, 161)]
    i = min(2, int(t*3)); f = t*3-i
    return '#'+''.join(f'{round(stops[i][j]*(1-f)+stops[i+1][j]*f):02x}' for j in range(3))


def csv_text(rows, fields):
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, fieldnames=fields, extrasaction='ignore')
    writer.writeheader(); writer.writerows(rows)
    return '\ufeff'+out.getvalue()


class Recorder:
    def __init__(self, root):
        self.root = Path(root).expanduser(); self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.folder = None
        self.file = None
        self.meta = None
        self.streams = {}
        self.last_sync = time.monotonic()
        self.context = {}

    def start(self, mode, route, settings, origin):
        with self.lock:
            if self.file:
                self.finish('interrupted','새 조사로 전환')
            mission = utc().replace(':', '').replace('.', '').replace('+0000', 'Z')+'_'+uuid.uuid4().hex[:6]
            self.folder = self.root/mission; self.folder.mkdir()
            self.file = (self.folder/'samples.csv').open('x', newline='', encoding='utf-8')
            self.writer = csv.DictWriter(self.file, fieldnames=FIELDS); self.writer.writeheader(); self.file.flush()
            self.meta = dict(schema_version=2, mission_id=mission, started_utc=utc(), status='running',
                             position_source=mode, do_source='sensor', route=route, settings=settings, origin=origin,
                             units={'do': 'mg/L', 'coordinates': 'WGS84 degrees', 'grid': 'm'},
                             color_scale={'mode': 'cumulative_grid_mean_minmax', 'meaning': 'DO concentration, not pollution class'},
                             positioning='latest host-received position <=2s old; no sensor response-lag correction',
                             experimental=mode != 'real', basemap='Esri World Imagery online tiles; exports contain coordinates, not raster tiles',
                             samples=0, calibration_changed=False,
                             runtime_context=self.context.copy(),
                             recording={'automatic':True,'screen_capture_required':False,
                                 'scope':'all messages received on subscribed GPS, IMU and LaserScan topics during mission; DO samples; navigation decisions; published commands',
                                 'telemetry_decimation':False,'fsync_interval_s':1,
                                 'nonfinite_encoding':{'NaN':'not a number','+Inf':'positive infinity','-Inf':'negative infinity'},
                                 'sensor_protocol_raw_directory':'../sensor_raw'},telemetry_counts={})
            self.save_meta(); self.event('start', {})
            return mission

    def save_meta(self):
        with self.lock:
            temp = self.folder/'mission.json.tmp'
            with temp.open('w',encoding='utf-8') as f:
                f.write(json.dumps(self.meta, ensure_ascii=False, indent=2));f.flush();os.fsync(f.fileno())
            temp.replace(self.folder/'mission.json')

    def event(self, kind, data):
        self.telemetry('events',dict(utc=utc(),event=kind,data=data))

    def telemetry(self, kind, data):
        with self.lock:
            if not self.file and self.folder and kind=='commands':
                # The final two seconds of neutral publication belong to the
                # same mission even after its sample file has been closed.
                with (self.folder/'commands.jsonl').open('a',encoding='utf-8') as f:
                    f.write(json.dumps(message_data(data),ensure_ascii=False,allow_nan=False)+'\n')
                    f.flush();os.fsync(f.fileno())
                counts=self.meta['telemetry_counts'];counts[kind]=counts.get(kind,0)+1
                self.save_meta()
            if self.file:
                # Keep every received record. Persistent line-buffered files avoid
                # reopening a file for every high-rate IMU or lidar frame.
                if kind not in self.streams:
                    self.streams[kind]=(self.folder/(kind+'.jsonl')).open('a',encoding='utf-8',buffering=1)
                self.streams[kind].write(json.dumps(message_data(data),ensure_ascii=False,allow_nan=False)+'\n')
                counts=self.meta['telemetry_counts'];counts[kind]=counts.get(kind,0)+1

    def sync(self,force=False):
        with self.lock:
            if self.file and (force or time.monotonic()-self.last_sync>=1):
                for f in [self.file,*self.streams.values()]:
                    f.flush();os.fsync(f.fileno())
                self.save_meta();self.last_sync=time.monotonic()

    def sample(self, row):
        with self.lock:
            if self.file:
                self.writer.writerow(row); self.file.flush()
                self.meta['samples'] += 1
                value = row['do_mg_l']
                bounds = self.meta.get('observed_do_range', [value,value])
                self.meta['observed_do_range'] = [min(bounds[0],value),max(bounds[1],value)]
                if self.meta['samples'] % 10 == 0:
                    self.save_meta()
                self.sync()

    def finish(self, status, reason):
        with self.lock:
            if self.file:
                self.meta.update(status=status, finished_utc=utc(), reason=reason)
                self.event(status, {'reason': reason})
                try:self.sync(force=True)
                finally:
                    for f in self.streams.values():f.close()
                    self.streams.clear();self.file.close();self.file=None

    def folders(self):
        results = []
        for p in sorted(self.root.glob('*/mission.json'), reverse=True)[:100]:
            try:
                results.append(json.loads(p.read_text(encoding='utf-8')))
            except (OSError, ValueError):
                pass
        return results

    def folder_for(self, mission):
        if not mission or any(c not in '0123456789TZ-_' for c in mission.split('_')[0]) or Path(mission).name != mission:
            raise ValueError('유효하지 않은 조사 ID')
        p = (self.root/mission).resolve()
        if p.parent != self.root.resolve() or not (p/'mission.json').is_file():
            raise ValueError('조사 기록이 없습니다.')
        return p

    def bundle(self, mission):
        with self.lock:
            folder = self.folder_for(mission)
            if folder == self.folder and self.file:
                self.sync(force=True)
            meta = json.loads((folder/'mission.json').read_text(encoding='utf-8'))
            raw=(folder/'samples.csv').read_text(encoding='utf-8',errors='replace')
            # Preserve source files and ignore a partial last row after a crash.
            complete=raw[:raw.rfind('\n')+1]
            rows=list(csv.DictReader(io.StringIO(complete)))
            meta['exported_samples']=len(rows)
            meta['incomplete_csv_tail_ignored']=len(complete)!=len(raw)
            cells = {}
            features = []
            for row in rows:
                if not row['lat'] or not row['lon']:
                    continue
                if row['cell_x'] != '' and row['cell_y'] != '':
                    key = (int(row['cell_x']), int(row['cell_y']))
                    c = cells.setdefault(key, {'ix': key[0], 'iy': key[1], 'values': []})
                    c['values'].append(float(row['do_mg_l']))
                features.append(dict(type='Feature', geometry=dict(type='Point', coordinates=[float(row['lon']), float(row['lat'])]),
                                     properties={**row, 'do_mg_l': float(row['do_mg_l'])}))
            gridrows, gridfeatures = [], []
            size = meta['settings']['cell_m']; origin = meta['origin']
            for c in cells.values():
                x, y = c['ix']*size, c['iy']*size
                right = min(x+size, meta['plan']['width']) if meta.get('plan') else x+size
                top = min(y+size, meta['plan']['height']) if meta.get('plan') else y+size
                corners = [latlon(px, py, origin) for px, py in [(x,y), (right,y), (right,top), (x,top), (x,y)]]
                v = c['values']
                r = dict(cell_x=c['ix'], cell_y=c['iy'], count=len(v), mean_mg_l=sum(v)/len(v),
                         min_mg_l=min(v), max_mg_l=max(v), position_source=meta['position_source'])
                gridrows.append(r)
                gridfeatures.append(dict(type='Feature', properties=r,
                                         geometry=dict(type='Polygon', coordinates=[[[p['lon'], p['lat']] for p in corners]])))
            route = dict(type='FeatureCollection', features=[dict(type='Feature', properties={'waypoint': i+1},
                         geometry=dict(type='Point', coordinates=[p['lon'], p['lat']])) for i,p in enumerate(meta['route'])])
            values = [c['mean_mg_l'] for c in gridrows]
            meta['color_scale'].update(min_mg_l=min(values) if values else None, max_mg_l=max(values) if values else None)
            # Full GPS/map-click track, independent of the DO sampling interval.
            track=[]
            trackfile=folder/('map_click.jsonl' if meta['position_source']=='simulated' else 'gps.jsonl')
            if meta.get('initial_position'):track.append(meta['initial_position'])
            if trackfile.exists():
                with trackfile.open(encoding='utf-8',errors='replace') as f:
                    for line in f:
                        if not line.endswith('\n'):continue
                        try:track.append(json.loads(line))
                        except ValueError:continue
            if not track:
                track=[dict(lat=float(r['lat']),lon=float(r['lon']),valid=True,received_utc=r['received_utc'])
                       for r in rows if r['lat'] and r['lon']]
            trackfeatures=[dict(type='Feature',geometry=dict(type='Point',coordinates=[p['lon'],p['lat']]),
                properties={k:v for k,v in p.items() if k not in ('lat','lon','mono')})
                for p in track if p.get('valid') and p.get('lat') is not None and p.get('lon') is not None]
            map_svg, chart_svg = figures(meta, rows, gridrows,track)
            page = report(meta, rows, map_svg, chart_svg)
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as z:
                for p in folder.iterdir():
                    if p.is_file() and p.name != 'mission.json' and p.suffix in ('.csv', '.json', '.jsonl'):
                        z.write(p, p.name)
                z.writestr('mission.json', json.dumps(meta, ensure_ascii=False, indent=2))
                for name, obj in [('samples.geojson', dict(type='FeatureCollection', features=features)),
                                  ('grid.geojson', dict(type='FeatureCollection', features=gridfeatures)), ('route.geojson', route),
                                  ('track.geojson',dict(type='FeatureCollection',features=trackfeatures))]:
                    z.writestr(name, json.dumps(obj, ensure_ascii=False))
                z.writestr('grid.csv', csv_text(gridrows, ['cell_x','cell_y','count','mean_mg_l','min_mg_l','max_mg_l','position_source']))
                z.writestr('map.svg', map_svg); z.writestr('timeseries.svg', chart_svg)
                z.writestr('report.html', page)
                z.writestr('README.txt', 'report.html을 브라우저로 열면 지도 격자와 시계열을 다시 볼 수 있습니다.\n'
                           'map.svg / timeseries.svg는 벡터 이미지입니다. samples.csv, grid.csv, GeoJSON으로 재분석할 수 있습니다.\n'
                           'position_source=simulated는 가상 위치입니다. 실제 하천 조사 결과로 사용할 수 없습니다.\n'
                           '지도 이미지는 좌표 기반이며 온라인 지도 배경 타일을 포함하지 않습니다. 색은 DO 농도이며 오염 등급이 아닙니다.\n')
            return stream.getvalue()


def figures(meta, rows, cells, track=()):
    mode = 'MANUAL POSITION / GPS OVERRIDE' if meta['experimental'] else 'FIELD GPS'
    header = '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="640" viewBox="0 0 1000 640"><rect width="1000" height="640" fill="#f5f7f8"/>'
    title = f'<text x="42" y="36" font-family="sans-serif" font-size="20">DO survey — {mode}</text>'
    size=meta['settings']['cell_m']; origin=meta['origin']
    pts = [xy(p, origin) for p in meta['route']]+[(r['cell_x']*size, r['cell_y']*size) for r in cells]
    pts += [xy(p,origin) for p in track if p.get('valid')]
    xs=[p[0] for p in pts] or [0]; ys=[p[1] for p in pts] or [0]
    left=min(xs)-size*2; bottom=min(ys)-size*2
    width=max(40,max(xs)-left+size*3); height=max(40,max(ys)-bottom+size*3)
    scale=min(890/width, 470/height)
    project=lambda x,y:(60+(x-left)*scale, 535-(y-bottom)*scale)
    parts=[header,title]
    means=[c['mean_mg_l'] for c in cells]
    low,high=(min(means),max(means)) if means else (0,0)
    for c in cells:
        left_edge,bottom_edge=c['cell_x']*size,c['cell_y']*size
        right=min(left_edge+size,meta['plan']['width']) if meta.get('plan') else left_edge+size
        top=min(bottom_edge+size,meta['plan']['height']) if meta.get('plan') else bottom_edge+size
        x,y=project(left_edge,top)
        parts.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{(right-left_edge)*scale:.2f}" height="{(top-bottom_edge)*scale:.2f}" fill="{color(c["mean_mg_l"], low, high)}" stroke="white" stroke-width="1"/>')
    route=[project(*xy(p,origin)) for p in meta['route']]
    if route:
        parts.append('<polyline points="'+' '.join(f'{x:.2f},{y:.2f}' for x,y in route)+'" fill="none" stroke="#253b49" stroke-width="2"/>')
    segments=[[]]
    for p in track:
        if p.get('valid'):segments[-1].append(project(*xy(p,origin)))
        elif segments[-1]:segments.append([])
    for segment in segments:
        if segment:parts.append('<polyline data-layer="observed-track" points="'+' '.join(f'{x:.2f},{y:.2f}' for x,y in segment)+'" fill="none" stroke="#078b80" stroke-width="2"/>')
    for i,(x,y) in enumerate(route):
        parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="9" fill="#273e4b"/><text x="{x:.2f}" y="{y+4:.2f}" text-anchor="middle" fill="white" font-family="sans-serif" font-size="11">{i+1}</text>')
    for i in range(100):
        parts.append(f'<rect x="{60+i*5}" y="570" width="5" height="14" fill="{color(low+i/99*(high-low), low, high)}"/>')
    parts.append(f'<text x="60" y="607" font-family="sans-serif" font-size="14">{low:.3f}                 DO (mg/L)                         {high:.3f} · cell {size:g} m · blank = unmeasured</text>')
    parts.append(f'<text x="60" y="555" font-family="sans-serif" font-size="12">WGS84 origin: {origin["lat"]:.7f}, {origin["lon"]:.7f} · North ↑ · Coordinate plot (no basemap)</text></svg>')
    map_svg=''.join(parts)
    vals=[float(r['do_mg_l']) for r in rows]
    from datetime import datetime
    times=[datetime.fromisoformat(r['received_utc']).timestamp() for r in rows]
    span=max(1,times[-1]-times[0]) if times else 1
    low=min(vals)-.2 if vals else 0; high=max(vals)+.2 if vals else 10
    plot=[]
    for t,v in zip(times, vals):
        plot.append((80+(t-times[0])/span*850, 530-(v-low)/(high-low)*430))
    chart=[header,title]
    for i in range(6):
        y=100+i*86; value=high-(high-low)*i/5
        chart.append(f'<line x1="80" y1="{y}" x2="930" y2="{y}" stroke="#d5dfe2"/><text x="20" y="{y+5}" font-family="sans-serif" font-size="14">{value:.2f}</text>')
    chart.append('<polyline fill="none" stroke="#187e79" stroke-width="3" points="'+' '.join(f'{x:.2f},{y:.2f}' for x,y in plot)+'"/>')
    chart.append(f'<text x="80" y="580" font-family="sans-serif" font-size="16">DO (mg/L) · elapsed seconds 0 → {span:.1f} · {len(vals)} samples</text></svg>')
    return map_svg, ''.join(chart)


def report(meta, rows, map_svg, chart_svg):
    title='수동 GPS 좌표 · DO 조사' if meta['experimental'] else '수질 조사 · 실제 GPS + 실제 DO'
    if rows and all(r.get('sensor')=='LOCAL_TEST' for r in rows):title='로컬 프로그램 시험 · 생성 데이터'
    safe=json.dumps(meta, ensure_ascii=False, indent=2)
    return f'''<!doctype html><html lang="ko"><meta charset="utf-8"><title>{title}</title>
    <style>body{{max-width:1060px;margin:40px auto;font-family:system-ui;line-height:1.7;color:#22343c;padding:20px}}svg{{width:100%;height:auto}}pre{{white-space:pre-wrap;background:#f2f5f6;padding:20px}}.tag{{padding:12px;background:#fff0cf}}@media print{{svg{{break-inside:avoid}}}}</style>
    <h1>{title}</h1><p class="tag">{html.escape(meta['mission_id'])} · {len(rows)}개 · 상태 {html.escape(meta['status'])}</p>
    <p>격자는 관측된 위치의 DO 평균입니다. 빈 칸은 미관측이며 보간하지 않았습니다. 낮은 DO는 추가 조사 대상이지 오염원 확정 결과가 아닙니다.</p>
    <h2>위치별 DO</h2>{map_svg}<h2>시간별 DO</h2>{chart_svg}
    <h2>기록 조건</h2><pre>{html.escape(safe)}</pre>
    <p>UTC 시간은 선박 수신 시각입니다. GPS는 가장 최근 수신값을 최대 2초 이내에서 결합했으며, 프로브 응답 지연은 보정하지 않았습니다.</p></html>'''
