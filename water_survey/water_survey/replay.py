"""Rebuild survey figures from a saved mission, without ROS, boat or browser."""
import argparse
import io
from pathlib import Path
import zipfile
from .recording import Recorder


def rebuild(mission_folder,output=None):
    folder=Path(mission_folder).expanduser().resolve()
    recorder=Recorder(folder.parent)
    data=recorder.bundle(folder.name)
    target=Path(output).expanduser().resolve() if output else folder/'visualization'
    if target==folder or folder.is_relative_to(target):
        raise ValueError('원본 조사 폴더 밖이나 그 안의 별도 출력 폴더를 지정하세요.')
    target.mkdir(parents=True,exist_ok=True)
    (target/'survey-export.zip').write_bytes(data)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in ['report.html','map.svg','timeseries.svg','grid.csv',
                     'samples.geojson','grid.geojson','route.geojson','track.geojson','mission.json','README.txt']:
            (target/name).write_bytes(z.read(name))
    return target


def main():
    parser=argparse.ArgumentParser(description='저장한 조사 폴더에서 지도·그래프·분석 ZIP 다시 만들기')
    parser.add_argument('mission_folder');parser.add_argument('--output')
    args=parser.parse_args()
    print(rebuild(args.mission_folder,args.output))


if __name__=='__main__':main()
