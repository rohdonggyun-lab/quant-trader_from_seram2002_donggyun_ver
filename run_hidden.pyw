"""창 없이 파이썬 스크립트를 실행한다 (작업 스케줄러용)

    pythonw.exe run_hidden.pyw run.py
    pythonw.exe run_hidden.pyw tools/team_export.py

pythonw 는 콘솔 창이 없어서 화면 출력이 사라지므로, 출력과 오류를 data/logs/<스크립트이름>.out 에 이어 쓴다.
(매매 기록과 알림 내용은 원래대로 data/logs/YYYYMMDD.log 와 슬랙에 남는다)
끄려면: 작업 관리자에서 pythonw.exe 를 끝내거나, PowerShell에서 Stop-ScheduledTask -TaskName '작업 이름'
"""
import os
import runpy
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
script = sys.argv[1]
logs = ROOT / 'data' / 'logs'
logs.mkdir(parents=True, exist_ok=True)
out = open(logs / f'{Path(script).stem}.out', 'a', encoding='utf-8', buffering=1)
sys.stdout = sys.stderr = out
print(f'\n===== {datetime.now():%Y-%m-%d %H:%M:%S} {script} 시작 (창 없이) =====')
sys.argv = [script] + sys.argv[2:]
try:
    runpy.run_path(script, run_name='__main__')
except SystemExit as ex:
    print(f'종료 코드 {ex.code}')
except BaseException:
    traceback.print_exc()
    raise
finally:
    print(f'===== {datetime.now():%Y-%m-%d %H:%M:%S} {script} 끝 =====')
