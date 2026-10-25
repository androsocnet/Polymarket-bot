import json,os,sqlite3,subprocess,time
from pathlib import Path
D=Path('/var/lib/polymarket-btc/phase2b-evidence/final-remediation-20261023T234452Z')
PREFIX='phase2b-final-234452'
def run(*args,check=True):
    return subprocess.run(args,check=check,capture_output=True,text=True,timeout=45).stdout.strip()
def props(unit):
    return dict(x.split('=',1) for x in run('systemctl','show',unit,'-p','ActiveState,MainPID,NRestarts,Result,ControlGroup').splitlines())
def arm(label,target,seconds,db=None):
    assert target in (PREFIX+'-worker.service',PREFIX+'-recovery.service','polymarket-btc-paper.service','polymarket-btc-infrastructure.service')
    folder=D/label; folder.mkdir()
    script=folder/'deadline.sh'
    script.write_text('#!/bin/bash\nset -u\nexec >>'+str(folder/'deadline.log')+' 2>&1\ndate -u --iso-8601=ns\nsystemctl stop '+target+'\nsystemctl show '+target+' -p ActiveState -p MainPID -p NRestarts -p Result\njournalctl -u '+target+' --no-pager -n 100 -o short-iso-precise > '+str(folder/'journal.log')+'\n'+(f"/usr/bin/python3 -c \"import sqlite3; s=sqlite3.connect('file:{db}?mode=ro',uri=True); d=sqlite3.connect('{folder}/deadline.sqlite'); s.backup(d); print(d.execute('pragma integrity_check').fetchall())\"\n" if db else '')+'date -u --iso-8601=ns\n')
    unit=PREFIX+'-'+label
    run('systemd-run','--unit='+unit,'--on-active='+str(seconds)+'s','--timer-property=AccuracySec=1s','--timer-property=RandomizedDelaySec=0','--property=Type=oneshot','--property=TimeoutStartSec=60','/bin/bash',str(script))
    status=run('systemctl','show',unit+'.timer','-p','ActiveState,NextElapseUSecMonotonic,TimersMonotonic')
    (folder/'armed.txt').write_text(status+'\n'+run('systemctl','cat',unit+'.timer',unit+'.service'))
    assert 'ActiveState=active' in status
    return unit,folder
if __name__=='__main__':
    worker=PREFIX+'-worker.service'; canary=PREFIX+'-canary.service'; observer=PREFIX+'-observer.service'
    for name in (worker,canary,observer):
        run('systemd-run','--unit='+name,'--property=Restart=no','/bin/sleep','120')
    started=time.monotonic(); guard,folder=arm('short-deadline',worker,8)
    assert props(worker)['ActiveState']=='active'
    run('systemctl','kill','--signal=SIGKILL',observer)
    assert run('systemctl','is-active',guard+'.timer')=='active'
    while props(worker)['MainPID']!='0' and time.monotonic()-started<20: time.sleep(.2)
    elapsed=time.monotonic()-started
    assert props(worker)['ActiveState']=='inactive' and elapsed<20
    while not (folder/'deadline.log').exists() and time.monotonic()-started<25: time.sleep(.2)
    assert (folder/'deadline.log').exists()
    assert props(canary)['ActiveState']=='active','guard stopped unrelated canary'
    assert 7<=elapsed<=15,elapsed
    result={'guard':guard,'target':worker,'elapsed_seconds':elapsed,'observer_killed':True,'guard_survived':True,'target_stopped':True,'unrelated_canary_active':True,'evidence_preserved':True}
    (folder/'result.json').write_text(json.dumps(result,indent=2))
    for unit in (worker,canary,observer,guard+'.timer',guard+'.service'):
        assert unit.startswith(PREFIX+'-')
        run('systemctl','stop',unit,check=False);run('systemctl','reset-failed',unit,check=False)
    print(json.dumps(result,indent=2))
