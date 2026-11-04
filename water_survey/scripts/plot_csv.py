#!/usr/bin/env python3
"""Regenerate figures from exported samples.csv; needs matplotlib."""
import argparse
import csv
from datetime import datetime
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('csv'); p.add_argument('--output',default='figures'); a=p.parse_args()
    with open(a.csv,encoding='utf-8-sig') as f: rows=list(csv.DictReader(f))
    if not rows: raise SystemExit('No samples')
    out=Path(a.output); out.mkdir(parents=True,exist_ok=True)
    virtual=any(r['position_source']=='simulated' for r in rows)
    label='INDOOR TEST - simulated GPS + real DO' if virtual else 'Field GPS + DO'
    times=[datetime.fromisoformat(r['received_utc']) for r in rows]
    fig,ax=plt.subplots(figsize=(10,4.5),constrained_layout=True)
    ax.plot(times,[float(r['do_mg_l']) for r in rows],color='#07897d')
    ax.set(title=label,xlabel='UTC receive time',ylabel='DO (mg/L)'); ax.grid(alpha=.25)
    for ext in ('png','svg'): fig.savefig(out/f'timeseries.{ext}',dpi=180)
    plt.close(fig)
    located=[r for r in rows if r['lat'] and r['lon']]
    if located:
        fig,ax=plt.subplots(figsize=(8,7),constrained_layout=True)
        m=ax.scatter([float(r['lon']) for r in located],[float(r['lat']) for r in located],
                     c=[float(r['do_mg_l']) for r in located],cmap='RdYlBu',vmin=2,vmax=12,s=25)
        fig.colorbar(m,ax=ax,label='DO (mg/L)'); ax.set(title=label,xlabel='Longitude (WGS84)',ylabel='Latitude (WGS84)')
        ax.ticklabel_format(useOffset=False); ax.grid(alpha=.2)
        for ext in ('png','svg'): fig.savefig(out/f'locations.{ext}',dpi=180)
        plt.close(fig)
    print(out.resolve())
if __name__=='__main__': main()
