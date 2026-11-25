"""Expose one NTRIP input-topic argument in the installed vendor launch, idempotently."""
from pathlib import Path
import sys

def configure(path):
    path=Path(path).resolve();text=path.read_text()
    if "DeclareLaunchArgument('ntrip_fix_topic'" in text:return False
    start=text.index('    ntrip_client_node = Node(');end=text.index('\n    #',start)
    block=text[start:end]
    old='remappings=[("fix", "gps/fix")]'
    assert old in block,'Unexpected vendor NTRIP launch; no change made'
    block=block.replace(old,'remappings=[("fix", LaunchConfiguration("ntrip_fix_topic", default="gps/fix"))]')
    text=text[:start]+block+text[end:]
    marker='    return LaunchDescription(\n        ['
    assert marker in text,'Unexpected launch description'
    text=text.replace(marker,marker+"\n            DeclareLaunchArgument('ntrip_fix_topic', default_value='gps/fix'),",1)
    backup=path.with_suffix(path.suffix+'.before-isabel-ntrip-seed')
    if not backup.exists():backup.write_text(path.read_text())
    path.write_text(text);return True

if __name__=='__main__':print('NTRIP_INPUT_TOPIC_CONFIGURED',configure(sys.argv[1]))
