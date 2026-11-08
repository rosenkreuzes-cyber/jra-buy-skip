import datetime as dt
import itertools
import io
import json
import sqlite3
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app import (JST, assess, choose4_from_top7, combo_key, create_snapshot,
                 db_connect, evaluate_history, export_zip, parse_odds_doc,
                 read_prediction_file, required_odds_for_hit_probability,
                 settle, store_snapshot, restore_zip, timing_check, win_popularity)

RACE='202605050109'
START=dt.datetime(2026,11,8,15,0,tzinfo=JST)
CAPTURE=dt.datetime(2026,11,8,14,45,tzinfo=JST)


def fixture_docs():
    win={'status':'result','data':{'official_datetime':'2026-11-08 14:43:00','odds':{'1':{str(k).zfill(2):[str(k+1.4),'',str(k)] for k in range(1,9)}}}}
    all_combos={''.join(str(n).zfill(2) for n in combo):[str(10+sum(combo)),str(i+1)] for i,combo in enumerate(itertools.combinations(range(1,9),3))}
    trio={'status':'result','data':{'official_datetime':'2026-11-08 14:42:00','odds':{'7':all_combos}}}
    return win,trio


def first_snapshot():
    win,trio=fixture_docs()
    return create_snapshot(race_id=RACE,start=START,capture=CAPTURE,win_doc=win,trio_doc=trio,
                           chosen4=(1,2,3,4),source='netkeiba_api',model_version='locked-v1',p_box=.32)


def test_parse_combo_and_prices():
    assert combo_key('010203') == (1,2,3)
    assert combo_key('2-11-4') == (2,4,11)
    assert combo_key('123') == (1,2,3)
    win, trio = fixture_docs()
    assert len(parse_odds_doc(win,1)) == 8
    assert len(parse_odds_doc(trio,7)) == 56
    assert parse_odds_doc(trio,7)[(1,2,3)]['odds'] == 16.0
    assert win_popularity(parse_odds_doc(win,1))[1] == 1


def test_file_selection():
    text = 'race_id,馬番,馬名,top3_probability\n'+''.join(f'{RACE},{i},H{i},{.9-i*.04:.4f}\n' for i in range(1,9))
    pred,dig=read_prediction_file(text.encode(),RACE)
    assert len(dig)==64
    assert choose4_from_top7(pred,{i:i for i in range(1,9)}) == (1,2,3,4)
    with pytest.raises(ValueError): read_prediction_file(b'foo,bar\n1,2\n',RACE)


def test_decisions_and_time():
    low={(1,2,3):3,(1,2,4):3.5,(1,3,4):3.2,(2,3,4):3.9}
    assert assess(low,.5,True)['status']=='配当不足・見送り'
    other={(1,2,3):10,(1,2,4):12,(1,3,4):15,(2,3,4):20}
    result=assess(other,.10,True)
    assert result['status'].startswith('配当不足')
    assert result['upper'] == pytest.approx(.5)
    assert required_odds_for_hit_probability(.32) == pytest.approx(12.5)
    assert assess(other,None,True)['status']=='判定保留'
    high={(1,2,3):30,(1,2,4):36,(1,3,4):38,(2,3,4):40}
    assert assess(high,.30,True)['status'].startswith('購入候補')
    assert assess(high,.30,False)['status']=='判定保留'
    assert timing_check(START,CAPTURE,CAPTURE-dt.timedelta(minutes=1),CAPTURE-dt.timedelta(minutes=2),'netkeiba_api')[0]
    assert not timing_check(START,START-dt.timedelta(minutes=2),None,None,'netkeiba_api')[0]
    assert not timing_check(START,CAPTURE,CAPTURE,CAPTURE,'unverified_json')[0]


def test_immutable_snapshot_and_settlement(tmp_path):
    con=db_connect(tmp_path/'test.sqlite')
    s=first_snapshot();store_snapshot(con,s)
    # Cannot switch 4 horses after first observation.
    different=dict(s);different['snapshot_id']='x';different['selected4']='[1,2,3,5]'
    with pytest.raises(ValueError,match='固定'):
        store_snapshot(con,different)
    # Another snapshot of the same race is permitted if horse set/model is unchanged.
    second=dict(s);second['snapshot_id']='4a2b8c05d19e47f3b6a2c7e0148d9f21';second['captured_at_jst']=(CAPTURE+dt.timedelta(minutes=1)).isoformat()
    store_snapshot(con,second)
    settle(con,RACE,(1,2,3),1360)
    with pytest.raises(ValueError,match='記録済'):
        settle(con,RACE,(1,2,3),1360)
    hist=evaluate_history(con)
    assert len(hist)==1 and bool(hist.iloc[0]['BOX的中'])
    assert int(hist.iloc[0]['公式払戻円'])==1360
    assert round(hist.iloc[0]['仮に全レース購入した回収率'],1)==340.0
    b=export_zip(con)
    assert len(b)>200
    newcon=db_connect(tmp_path/'new.sqlite')
    assert restore_zip(newcon,b)==(2,1)
    assert restore_zip(newcon,b)==(0,0)
    assert len(evaluate_history(newcon))==1
    newcon.close()
    con.close()


def test_unverified_and_invalid():
    win,trio=fixture_docs()
    s=create_snapshot(race_id=RACE,start=START,capture=CAPTURE,win_doc=win,trio_doc=trio,
        chosen4=(1,2,3,4),source='unverified_json')
    assert not bool(s['timing_verified'])
    shifted=create_snapshot(race_id=RACE,start=START,capture=CAPTURE,win_doc=win,trio_doc=trio,
            chosen4=(1,2,3,8),source='netkeiba_api')
    assert shifted['decision']=='人気条件逸脱・見送り' 
    del trio['data']['odds']['7']['010203']
    with pytest.raises(ValueError,match='不足'):
        create_snapshot(race_id=RACE,start=START,capture=CAPTURE,win_doc=win,trio_doc=trio,
            chosen4=(1,2,3,4),source='netkeiba_api')


def test_joint_probability_proxy_is_explicitly_unvalidated():
    from app import estimate_box_probability_unvalidated
    race=RACE
    text='race_id,馬番,top3_probability\n'+''.join(f'{race},{i},{.55-i*.04:.4f}\n' for i in range(1,9))
    p,_=read_prediction_file(text.encode(),race)
    p_box,q4=estimate_box_probability_unvalidated(p,(1,2,3,4))
    assert len(q4)==4 and 0<p_box<1
    assert abs(sum(q4.values())-p_box)<1e-10
    win,trio=fixture_docs()
    s=create_snapshot(race_id=race,start=START,capture=CAPTURE,win_doc=win,trio_doc=trio,
        chosen4=(1,2,3,4),source='netkeiba_api',combo_probs4=q4)
    assert s['probability_source'].startswith('3頭集合')
    assert s['model_ev_reference'] is not None
