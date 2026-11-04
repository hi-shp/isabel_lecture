import csv
import html
import io
import json
from pathlib import Path
import threading
import uuid
import zipfile
from .core import utc, latlon, xy

FIELDS = ['sequence', 'request_utc', 'received_utc', 'do_mg_l', 'lat', 'lon', 'position_source',
          'do_source', 'gps_valid', 'gps_age_s', 'gps_stamp', 'heading_deg', 'waypoint', 'phase',
          'response_ms', 'sensor', 'firmware', 'payload_hex', 'cell_x', 'cell_y']


def color(value):
    t = max(0, min(1, (value-2)/10))
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
        self.last_imu = ''

    def start(self, mode, route, settings, origin):
        with self.lock:
            if self.file:
                self.file.close()
            mission = utc().replace(':', '').replace('.', '').replace('+0000', 'Z')+'_'+uuid.uuid4().hex[:6]
            self.folder = self.root/mission; self.folder.mkdir()
            self.file = (self.folder/'samples.csv').open('x', newline='', encoding='utf-8')
            self.writer = csv.DictWriter(self.file, fieldnames=FIELDS); self.writer.writeheader(); self.file.flush()
            self.meta = dict(schema_version=1, mission_id=mission, started_utc=utc(), status='running',
                             position_source=mode, do_source='sensor', route=route, settings=settings, origin=origin,
                             units={'do': 'mg/L', 'coordinates': 'WGS84 degrees', 'grid': 'm'},
                             color_scale={'min_mg_l': 2, 'max_mg_l': 12, 'meaning': 'DO concentration, not pollution class'},
                             positioning='latest host-received position <=2s old; no sensor response-lag correction',
                             experimental=mode != 'real', basemap='OpenStreetMap online tiles; exports contain coordinates, not raster tiles',
                             samples=0, calibration_changed=False)
            self.save_meta(); self.event('start', {})
            return mission

    def save_meta(self):
        temp = self.folder/'mission.json.tmp'
        temp.write_text(json.dumps(self.meta, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.folder/'mission.json')

    def event(self, kind, data):
        if self.folder:
            with (self.folder/'events.jsonl').open('a', encoding='utf-8') as f:
                f.write(json.dumps(dict(utc=utc(), event=kind, data=data), ensure_ascii=False)+'\n')

    def telemetry(self, kind, data):
        with self.lock:
            if self.file:
                # These are raw telemetry receipt records, independent of the joined DO rows.
                if kind == 'imu':
                    bucket = data['received_utc'][:21]
                    if bucket == self.last_imu:
                        return
                    self.last_imu = bucket
                with (self.folder/(kind+'.jsonl')).open('a', encoding='utf-8') as f:
                    f.write(json.dumps(data, ensure_ascii=False, allow_nan=False)+'\n')

    def sample(self, row):
        with self.lock:
            if self.file:
                self.writer.writerow(row); self.file.flush()
                self.meta['samples'] += 1
                if self.meta['samples'] % 10 == 0:
                    self.save_meta()

    def finish(self, status, reason):
        with self.lock:
            if self.file:
                self.meta.update(status=status, finished_utc=utc(), reason=reason)
                self.save_meta(); self.event(status, {'reason': reason})
                self.file.close(); self.file = None

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
                self.file.flush(); self.save_meta()
            meta = json.loads((folder/'mission.json').read_text(encoding='utf-8'))
            with (folder/'samples.csv').open(encoding='utf-8') as sample_file:
                rows = list(csv.DictReader(sample_file))
            cells = {}
            features = []
            for row in rows:
                if not row['lat'] or not row['lon']:
                    continue
                key = (int(row['cell_x']), int(row['cell_y']))
                c = cells.setdefault(key, {'ix': key[0], 'iy': key[1], 'values': []})
                c['values'].append(float(row['do_mg_l']))
                features.append(dict(type='Feature', geometry=dict(type='Point', coordinates=[float(row['lon']), float(row['lat'])]),
                                     properties={**row, 'do_mg_l': float(row['do_mg_l'])}))
            gridrows, gridfeatures = [], []
            size = meta['settings']['cell_m']; origin = meta['origin']
            for c in cells.values():
                x, y = c['ix']*size, c['iy']*size
                corners = [latlon(px, py, origin) for px, py in [(x,y), (x+size,y), (x+size,y+size), (x,y+size), (x,y)]]
                v = c['values']
                r = dict(cell_x=c['ix'], cell_y=c['iy'], count=len(v), mean_mg_l=sum(v)/len(v),
                         min_mg_l=min(v), max_mg_l=max(v), position_source=meta['position_source'])
                gridrows.append(r)
                gridfeatures.append(dict(type='Feature', properties=r,
                                         geometry=dict(type='Polygon', coordinates=[[[p['lon'], p['lat']] for p in corners]])))
            route = dict(type='FeatureCollection', features=[dict(type='Feature', properties={'waypoint': i+1},
                         geometry=dict(type='Point', coordinates=[p['lon'], p['lat']])) for i,p in enumerate(meta['route'])])
            map_svg, chart_svg = figures(meta, rows, gridrows)
            page = report(meta, rows, map_svg, chart_svg)
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as z:
                for p in folder.iterdir():
                    if p.is_file() and p.suffix in ('.csv', '.json', '.jsonl'):
                        z.write(p, p.name)
                for name, obj in [('samples.geojson', dict(type='FeatureCollection', features=features)),
                                  ('grid.geojson', dict(type='FeatureCollection', features=gridfeatures)), ('route.geojson', route)]:
                    z.writestr(name, json.dumps(obj, ensure_ascii=False))
                z.writestr('grid.csv', csv_text(gridrows, ['cell_x','cell_y','count','mean_mg_l','min_mg_l','max_mg_l','position_source']))
                z.writestr('map.svg', map_svg); z.writestr('timeseries.svg', chart_svg)
                z.writestr('report.html', page)
                z.writestr('README.txt', 'report.html을 브라우저로 열면 지도 격자와 시계열을 다시 볼 수 있습니다.\n'
                           'map.svg / timeseries.svg는 벡터 이미지입니다. samples.csv, grid.csv, GeoJSON으로 재분석할 수 있습니다.\n'
                           'position_source=simulated는 가상 위치입니다. 실제 하천 조사 결과로 사용할 수 없습니다.\n'
                           '지도 이미지는 좌표 기반이며 온라인 지도 배경 타일을 포함하지 않습니다. 색은 DO 농도이며 오염 등급이 아닙니다.\n')
            return stream.getvalue()


def figures(meta, rows, cells):
    mode = 'INDOOR TEST / SIMULATED GPS' if meta['experimental'] else 'FIELD GPS'
    header = '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="640" viewBox="0 0 1000 640"><rect width="1000" height="640" fill="#f5f7f8"/>'
    title = f'<text x="42" y="36" font-family="sans-serif" font-size="20">DO survey — {mode}</text>'
    size=meta['settings']['cell_m']; origin=meta['origin']
    pts = [xy(p, origin) for p in meta['route']]+[(r['cell_x']*size, r['cell_y']*size) for r in cells]
    xs=[p[0] for p in pts] or [0]; ys=[p[1] for p in pts] or [0]
    left=min(xs)-size*2; bottom=min(ys)-size*2
    width=max(40,max(xs)-left+size*3); height=max(40,max(ys)-bottom+size*3)
    scale=min(890/width, 470/height)
    project=lambda x,y:(60+(x-left)*scale, 535-(y-bottom)*scale)
    parts=[header,title]
    for c in cells:
        x,y=project(c['cell_x']*size,(c['cell_y']+1)*size)
        parts.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{size*scale:.2f}" height="{size*scale:.2f}" fill="{color(c["mean_mg_l"])}" stroke="white" stroke-width="1"/>')
    route=[project(*xy(p,origin)) for p in meta['route']]
    if route:
        parts.append('<polyline points="'+' '.join(f'{x:.2f},{y:.2f}' for x,y in route)+'" fill="none" stroke="#253b49" stroke-width="2"/>')
    for i,(x,y) in enumerate(route):
        parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="9" fill="#273e4b"/><text x="{x:.2f}" y="{y+4:.2f}" text-anchor="middle" fill="white" font-family="sans-serif" font-size="11">{i+1}</text>')
    for i in range(100):
        parts.append(f'<rect x="{60+i*5}" y="570" width="5" height="14" fill="{color(2+i/99*10)}"/>')
    parts.append(f'<text x="60" y="607" font-family="sans-serif" font-size="14">2                 DO (mg/L)                         12 · cell {size:g} m · blank = unmeasured</text>')
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
    title='실내 시험 · 가상 GPS + 실제 DO' if meta['experimental'] else '수질 조사 · 실제 GPS + 실제 DO'
    safe=json.dumps(meta, ensure_ascii=False, indent=2)
    return f'''<!doctype html><html lang="ko"><meta charset="utf-8"><title>{title}</title>
    <style>body{{max-width:1060px;margin:40px auto;font-family:system-ui;line-height:1.7;color:#22343c;padding:20px}}svg{{width:100%;height:auto}}pre{{white-space:pre-wrap;background:#f2f5f6;padding:20px}}.tag{{padding:12px;background:#fff0cf}}@media print{{svg{{break-inside:avoid}}}}</style>
    <h1>{title}</h1><p class="tag">{html.escape(meta['mission_id'])} · {len(rows)}개 · 상태 {html.escape(meta['status'])}</p>
    <p>격자는 관측된 위치의 DO 평균입니다. 빈 칸은 미관측이며 보간하지 않았습니다. 낮은 DO는 추가 조사 대상이지 오염원 확정 결과가 아닙니다.</p>
    <h2>위치별 DO</h2>{map_svg}<h2>시간별 DO</h2>{chart_svg}
    <h2>기록 조건</h2><pre>{html.escape(safe)}</pre>
    <p>UTC 시간은 선박 수신 시각입니다. GPS는 가장 최근 수신값을 최대 2초 이내에서 결합했으며, 프로브 응답 지연은 보정하지 않았습니다.</p></html>'''
