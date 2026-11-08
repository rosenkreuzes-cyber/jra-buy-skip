#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JRA dirt 3連複4頭BOX: 発走前オッズ記録/買う・見送る研究アプリ.

Standalone: streamlit run app.py
This deliberately DOES NOT claim profitable bets. Snapshots are immutable.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import itertools
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import uuid
import zipfile
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np
import requests

JST = ZoneInfo("Asia/Tokyo")
DB_PATH = Path(os.environ.get("JRA_ODDS_DB_PATH", str(Path(__file__).resolve().parent / "data" / "odds_research.sqlite")))
USER_AGENT = "Mozilla/5.0 (compatible; JRA-Research-OddsSnapshot/1.0)"
ODDS_ENDPOINT = "https://race.netkeiba.com/api/api_get_jra_odds.html"
CUTOFF_MINUTES = 5
MAX_STALE_MINUTES = 20
RESEARCH_MARGIN = .15  # 研究開始前に仮固定した安全余裕。過去成績で随時調整しない
UNIT = 100
BOX_COST = 400
PLACEHOLDER_DECISION = "判定保留"


def now_jst() -> dt.datetime:
    return dt.datetime.now(JST)


def as_jst(ts: str | dt.datetime) -> dt.datetime:
    if isinstance(ts, str):
        value = str(ts).strip().replace(" ", "T", 1)
        t = dt.datetime.fromisoformat(value)
    else:
        t = ts
    return t.replace(tzinfo=JST) if t.tzinfo is None else t.astimezone(JST)


def source_timestamp(doc: dict) -> dt.datetime | None:
    info = doc.get("data") or {}
    if not isinstance(info, dict):
        return None
    for key in ("official_datetime", "update_time", "updated_at"):
        ts = info.get(key)
        if ts:
            try:
                return as_jst(str(ts))
            except (TypeError, ValueError):
                pass
    return None


def valid_race_id(race_id: str) -> str:
    race_id = str(race_id).strip()
    if not re.fullmatch(r"20\d{10}", race_id):
        raise ValueError("netkeibaの12桁レースIDを入力してください。")
    if not (1 <= int(race_id[4:6]) <= 10 and 1 <= int(race_id[-2:]) <= 12):
        raise ValueError("レースIDの競馬場・レース番号が不正です。")
    return race_id


def combo_key(raw: object) -> tuple[int, int, int]:
    key = str(raw).strip()
    if re.fullmatch(r"\d{6}", key):
        arr = [int(key[i:i+2]) for i in (0, 2, 4)]
    elif re.fullmatch(r"\d{3}", key):
        arr = [int(key[i]) for i in (0, 1, 2)]
    else:
        parts = re.findall(r"\d+", key)
        if len(parts) != 3:
            raise ValueError(f"3連複の組番キーを解読できません: {key}")
        arr = [int(v) for v in parts]
    if len(set(arr)) != 3 or not all(1 <= n <= 18 for n in arr):
        raise ValueError(f"3連複の組番が不正です: {key}")
    return tuple(sorted(arr))


def first_positive_float(value: object, *, minimum: float = 0.0) -> float | None:
    if isinstance(value, (tuple, list)):
        value = value[0] if len(value) else None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) and out > minimum else None


def parse_odds_doc(document: dict, kind: int) -> dict:
    if not isinstance(document, dict):
        raise ValueError("オッズの応答がJSON形式ではありません。")
    data = document.get("data") or {}
    odds = data.get("odds") if isinstance(data, dict) else None
    if not isinstance(odds, dict):
        raise ValueError(f"券種{kind}のオッズ一覧が取得できません（発売前・通信エラーの可能性）。")
    group = odds.get(str(kind))
    if not isinstance(group, dict) or not group:
        raise ValueError(f"券種{kind}のオッズがありません。発売前や取得制限の可能性があります。")
    parsed = {}
    bad = 0
    for raw_key, value in group.items():
        try:
            key = combo_key(raw_key) if kind == 7 else int(str(raw_key))
            if kind == 1 and not 1 <= key <= 18:
                raise ValueError("馬番不正")
            min_value = 1.0 if kind == 1 else 0.0
            odd = first_positive_float(value, minimum=min_value - 1e-10)
            if odd is None or odd < 1.0:  # 1.0 未満は異常、売止め等は除外
                bad += 1
                continue
            if key in parsed:
                raise ValueError(f"重複したキー: {key}")
            if kind == 1:
                reported_rank = None
                if isinstance(value, list) and len(value) > 2:
                    try:
                        reported_rank = int(value[2])
                    except (TypeError, ValueError):
                        pass
                parsed[key] = {"odds": odd, "source_rank": reported_rank}
            else:
                reported_rank = None
                if isinstance(value, list) and len(value) > 1:
                    try:
                        reported_rank = int(value[-1])
                    except (TypeError, ValueError):
                        pass
                parsed[key] = {"odds": odd, "source_rank": reported_rank}
        except (ValueError, TypeError):
            bad += 1
    if not parsed:
        raise ValueError(f"券種{kind}に有効なオッズがありません。無効レコード={bad}")
    return parsed


def fetch_netkeiba(race_id: str, kind: int) -> dict:
    race_id = valid_race_id(race_id)
    params = {"race_id": race_id, "type": str(kind), "action": "update"}
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/javascript,*/*;q=0.9",
        "Referer": f"https://race.netkeiba.com/odds/index.html?race_id={race_id}&type=b{kind}",
    }
    response = requests.get(ODDS_ENDPOINT, params=params, headers=headers, timeout=12)
    response.raise_for_status()
    try:
        result = response.json()
    except ValueError as e:
        raise RuntimeError("オッズAPIがJSON以外を返しました。アクセス制限・仕様変更の可能性があります。") from e
    parse_odds_doc(result, kind)  # strict validation
    return result


def win_popularity(win_odds: dict) -> dict[int, int]:
    # Use explicitly supplied ranks only if credible and unique.
    declared = {n: v.get("source_rank") for n, v in win_odds.items()}
    ranks = list(declared.values())
    if all(isinstance(x, int) and x > 0 for x in ranks) and len(set(ranks)) == len(ranks):
        return declared
    return {no: index for index, no in enumerate(sorted(win_odds, key=lambda n: (win_odds[n]["odds"], n)), 1)}


def read_prediction_file(raw: bytes, race_id: str) -> tuple[pd.DataFrame, str]:
    digest = hashlib.sha256(raw).hexdigest()
    try:
        df = pd.read_csv(io.BytesIO(raw), encoding="utf-8-sig", dtype={"race_id": str})
    except Exception as e:
        raise ValueError("モデルの予測CSVが読めません。UTF-8のCSVが必要です。") from e
    if "race_id" in df.columns:
        df = df[df["race_id"].astype(str).str.strip() == race_id].copy()
        if df.empty:
            raise ValueError("CSV内に指定したレースIDの予測がありません。")
    if "馬番" not in df.columns:
        raise ValueError("予測CSVには『馬番』列が必要です。")
    pcol = next((x for x in ("top3_probability", "p_full_race3", "p_top3", "3着以内確率") if x in df.columns), None)
    if pcol is None and "model_rank" not in df.columns:
        raise ValueError("『top3_probability』または『model_rank』列が必要です。")
    df["馬番"] = pd.to_numeric(df["馬番"], errors="coerce")
    if df["馬番"].isna().any() or df["馬番"].duplicated().any() or not df["馬番"].between(1, 18).all():
        raise ValueError("予測CSVの馬番が欠損・重複・不正です。")
    df["馬番"] = df["馬番"].astype(int)
    if pcol:
        df["_sortp"] = pd.to_numeric(df[pcol], errors="coerce")
        if df["_sortp"].isna().any() or not df["_sortp"].between(0, 1).all():
            raise ValueError("3着以内確率は0〜1の数値で指定してください。")
    else:
        df["_sortp"] = -pd.to_numeric(df["model_rank"], errors="coerce")
    if df["_sortp"].isna().any():
        raise ValueError("モデル順位に数値以外が含まれます。")
    df.attrs['prob_column']=pcol
    return df, digest


def estimate_box_probability_unvalidated(predictions: pd.DataFrame,
                                         selected4: tuple[int,int,int,int]) -> tuple[float,dict]:
    """Maximum-entropy combination proxy fitted to individual top3 marginals.
    IMPORTANT: output is not independently calibrated; never present as established odds edge.
    """
    if not predictions.attrs.get('prob_column'):
        raise ValueError('モデル順位だけでは3連複BOX確率を計算できません。')
    if len(predictions) < 5 or not set(selected4).issubset(set(predictions['馬番'])):
        raise ValueError('全馬の予測値が不足しています。')
    # Preserve input file's order and use a probability consistent with exactly three places.
    horse_nos=predictions['馬番'].astype(int).tolist()
    p=predictions['_sortp'].to_numpy(float)
    p=np.clip(p,1e-6,1-1e-6)
    p=p*3.0/p.sum()
    if (p>=1).any():
        raise ValueError('上位馬の確率が大きすぎて周辺確率を正規化できません。')
    combos=np.array(list(itertools.combinations(range(len(p)),3)),dtype=int)
    theta=np.log(p)
    for _ in range(700):
        w=np.exp(theta-theta.max());w/=w.sum()
        q=np.prod(w[combos],axis=1);q/=q.sum()
        approx=np.bincount(combos.ravel(),weights=np.repeat(q,3),minlength=len(p))
        if np.max(np.abs(approx-p)) <= 5e-7:
            break
        theta += .8*(np.log(p)-np.log(np.maximum(approx,1e-12)))
    if np.max(np.abs(approx-p)) > 2e-4:
        raise ValueError('組み合わせ確率の推定が収束しません。')
    q4={}
    four=set(selected4)
    for c,prob in zip(combos,q):
        nums=tuple(sorted(horse_nos[i] for i in c))
        if set(nums).issubset(four):
            q4[nums]=float(prob)
    if len(q4)!=4:
        raise ValueError('3連複の4組に確率を割り当てられません。')
    return sum(q4.values()),q4


def choose4_from_top7(predictions: pd.DataFrame, ranks: dict[int, int]) -> tuple[int, int, int, int]:
    eligible = predictions[predictions["馬番"].map(ranks).le(7).fillna(False)].copy()
    eligible = eligible.sort_values(["_sortp", "馬番"], ascending=[False, True])
    if len(eligible) < 4:
        raise ValueError("人気上位7頭に、4頭分の予測データが揃っていません。")
    return tuple(sorted(eligible.head(4)["馬番"].astype(int)))


def parse_four_numbers(txt: str) -> tuple[int, int, int, int]:
    nums = [int(n) for n in re.findall(r"\d+", str(txt))]
    if len(nums) != 4 or len(set(nums)) != 4 or not all(1 <= n <= 18 for n in nums):
        raise ValueError("選択馬番は異なる4頭を入力してください（例：1, 3, 7, 12）。")
    return tuple(sorted(nums))


def required_odds_for_hit_probability(p_box: float) -> float:
    if not 0.0 < p_box <= 1.0:
        raise ValueError("BOX的中確率は0より大きく100％以下で指定してください。")
    return 4.0 / p_box


def assess(odds4: dict[tuple[int, int, int], float], p_box: float | None,
           verified_snapshot: bool, *, margin: float = .15) -> dict:
    """Conservative interval, DOES NOT presume distribution among winning triples.

    E[return]/stake ∈ [p_box * min_odds/4, p_box * max_odds/4].
    Any 'candidate' remains research-only: p_box external calibration unverified.
    """
    values = [float(x) for x in odds4.values()]
    if len(values) != 4 or any(not math.isfinite(x) or x < 1 for x in values):
        return dict(status="判定保留", reason="3連複4組の実オッズが揃っていません。", lower=None, upper=None)
    low, high = min(values), max(values)
    if high <= 4:
        return dict(status="配当不足・見送り", reason="4組すべて的中しても払戻が400円以下。確率を問わず利益なし。",lower=None, upper=None)
    if p_box is None:
        return dict(status="判定保留", reason="当該BOXの校正済み的中確率がありません。4組の価格だけ記録します。",lower=None, upper=None)
    lower, upper = p_box * low / 4, p_box * high / 4
    if upper < 1.0:
        status="配当不足・見送り（仮説）"
        reason="入力されたBOX的中確率が正しければ、最も高い配当でも期待回収率100％に届きません。"
    elif lower >= 1.0 + margin and verified_snapshot:
        status="購入候補（未実証・研究用）"
        reason="入力確率を信じる場合、4組のどれが当たっても目標余裕率に到達。ただし確率の校正と利益は未検証。"
    else:
        status="判定保留"
        reason="当たり組の条件付き確率が不明で、4組の価格だけでは収益性を判定できません。"
    if not verified_snapshot and status.startswith("購入候補"):
        status="判定保留"; reason="発走前の時刻検証ができていません。"
    return dict(status=status,reason=reason,lower=lower,upper=upper)


def timing_check(start: dt.datetime, capture: dt.datetime, t_win: dt.datetime | None,
                 t_trio: dt.datetime | None, source: str) -> tuple[bool, str]:
    cutoff = start - dt.timedelta(minutes=CUTOFF_MINUTES)
    if capture >= cutoff:
        return False, f"取得が発走{CUTOFF_MINUTES}分前の締切を過ぎています。"
    if start-capture > dt.timedelta(hours=24):
        return False, "24時間以上前の取得のため、購入判断には使用しません。"
    if source != "netkeiba_api":
        return False, "手入力・ファイル入力は発走前の配信時刻を独立検証できません。"
    if not (t_win and t_trio):
        return False, "配信元のオッズ更新時刻が片方または両方とも不明です。"
    for label, ts in [("単勝", t_win), ("3連複", t_trio)]:
        if ts >= cutoff:
            return False, f"{label}オッズの公式時刻が発走{CUTOFF_MINUTES}分前を過ぎています。"
        delay = (capture-ts).total_seconds()/60
        if delay < -1 or delay > MAX_STALE_MINUTES:
            return False, f"{label}オッズの更新時刻と取得時刻が大きく離れています（{delay:.1f}分）。"
    return True, "発走5分以上前に取得し、配信更新時刻も確認できました。"


def create_snapshot(*, race_id: str, start: dt.datetime, capture: dt.datetime, win_doc: dict,
                    trio_doc: dict, chosen4: tuple[int,...], source: str,
                    model_digest: str = "", model_version: str = "", p_box: float | None = None,
                    margin: float = .15, manual_note: str = "",
                    combo_probs4: dict | None = None) -> dict:
    valid_race_id(race_id)
    start, capture = as_jst(start), as_jst(capture)
    if int(race_id[:4]) != start.year:
        raise ValueError('レースIDの開催年と入力した開催日が一致しません。')
    selected4 = tuple(sorted(chosen4))
    if len(selected4)!=4 or len(set(selected4))!=4:
        raise ValueError("選択馬が4頭ではありません。")
    wins = parse_odds_doc(win_doc,1)
    trios = parse_odds_doc(trio_doc,7)
    ranks = win_popularity(wins)
    if any(no not in ranks for no in selected4):
        raise ValueError("選択4頭の単勝オッズが不足しています。")
    # A set that was selected within the top7 must never be silently reselected
    # if the live ranks subsequently change. Report it, do not mutate it.
    rank_outside = [no for no in selected4 if ranks[no] > 7]
    keys = list(itertools.combinations(selected4, 3))
    missing = [key for key in keys if key not in trios]
    if missing:
        raise ValueError(f"3連複オッズが不足：{missing}。未発売・除外などを確認してください。")
    odds4 = {key:trios[key]["odds"] for key in keys}
    if combo_probs4:
        if set(combo_probs4)!=set(odds4) or not all(0<=p<=1 for p in combo_probs4.values()):
            raise ValueError('組み合わせ確率が選択4頭と一致しません。')
        derived_pbox=sum(combo_probs4.values())
        if p_box is None:
            p_box=derived_pbox
        elif abs(p_box-derived_pbox)>1e-3:
            # A user-supplied p_box must not be mixed with a different q distribution.
            combo_probs4=None
    two = (source_timestamp(win_doc), source_timestamp(trio_doc))
    verified, time_reason = timing_check(start,capture,*two,source)
    conclusion=assess(odds4,p_box,verified,margin=margin)
    model_ev=(sum(combo_probs4[key]*odds4[key] for key in keys)/4) if combo_probs4 else None
    if rank_outside:
        conclusion["status"] = "人気条件逸脱・見送り"
        conclusion["reason"] = f"初回に固定した選択馬が現在の人気上位7頭から外れました: {rank_outside}。選択馬は変更せず記録します。"
    if not verified and conclusion["status"] == "購入候補（未実証・研究用）":
        conclusion["status"] = PLACEHOLDER_DECISION
    return {
      "snapshot_id": uuid.uuid4().hex,
      "race_id":race_id,"start_jst":start.isoformat(),"captured_at_jst":capture.isoformat(),
      "official_win_jst":two[0].isoformat() if two[0] else "",
      "official_trio_jst":two[1].isoformat() if two[1] else "",
      "source":source,"model_digest":model_digest,"model_version":model_version,
      "selected4":json.dumps(list(selected4),ensure_ascii=False),
      "selected_rank":json.dumps({str(no):ranks[no] for no in selected4},ensure_ascii=False),
      "trio_odds4":json.dumps({"-".join(map(str,k)):v for k,v in odds4.items()},ensure_ascii=False),
      "all_trio_odds":json.dumps({"-".join(map(str,k)):v["odds"] for k,v in trios.items()},ensure_ascii=False),
      "win_odds":json.dumps({str(no):v for no,v in wins.items()},ensure_ascii=False),
      "box_probability":p_box,
      "probability_source":("3頭集合の最大エントロピー近似・未校正" if combo_probs4
          else ("外部入力・校正未検証" if p_box is not None else "なし")),
      "combo_probs4":json.dumps({"-".join(map(str,k)):v for k,v in combo_probs4.items()},ensure_ascii=False) if combo_probs4 else "",
      "model_ev_reference":model_ev,
      "decision":conclusion["status"],"decision_reason":conclusion["reason"],
      "ev_lower_bound":conclusion["lower"],"ev_upper_bound":conclusion["upper"],
      "timing_verified":int(verified),"timing_note":time_reason,
      "threshold_margin":margin,"manual_note":str(manual_note)[:500]
    }


def db_connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(str(path),timeout=8)
    con.row_factory=sqlite3.Row
    con.execute('''CREATE TABLE IF NOT EXISTS snapshots (
       snapshot_id TEXT PRIMARY KEY,
       race_id TEXT NOT NULL,
       start_jst TEXT NOT NULL,
       captured_at_jst TEXT NOT NULL,
       official_win_jst TEXT,
       official_trio_jst TEXT,
       source TEXT NOT NULL,
       model_digest TEXT,
       model_version TEXT,
       selected4 TEXT NOT NULL,
       selected_rank TEXT NOT NULL,
       trio_odds4 TEXT NOT NULL,
       all_trio_odds TEXT NOT NULL,
       win_odds TEXT NOT NULL,
       box_probability REAL,
       probability_source TEXT,
       combo_probs4 TEXT,
       model_ev_reference REAL,
       decision TEXT,
       decision_reason TEXT,
       ev_lower_bound REAL,
       ev_upper_bound REAL,
       timing_verified INTEGER,
       timing_note TEXT,
       threshold_margin REAL,
       manual_note TEXT
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS results (
        race_id TEXT PRIMARY KEY,
        winning_combo TEXT NOT NULL,
        official_payout_yen100 INTEGER NOT NULL,
        settled_at_jst TEXT NOT NULL,
        note TEXT
    )''')
    con.commit()
    return con


def store_snapshot(con: sqlite3.Connection, s: dict) -> None:
    # Lock the original horse set and input model version for the entire race.
    first=con.execute("SELECT selected4,model_digest,model_version,start_jst,box_probability,threshold_margin FROM snapshots WHERE race_id=? ORDER BY captured_at_jst LIMIT 1",(s["race_id"],)).fetchone()
    if first:
        if tuple(json.loads(first['selected4'])) != tuple(json.loads(s['selected4'])):
            raise ValueError("選択4頭はこのレースですでに固定されています。途中で変更できません。")
        if first['model_digest'] != s['model_digest'] or first['model_version'] != s['model_version']:
            raise ValueError("このレースのモデル・予測CSVはすでに固定されています。途中で変更できません。")
        if first['start_jst'] != s['start_jst']:
            raise ValueError("発走予定時刻が記録済みと異なります。変更履歴を別途管理してください。")
        if first['box_probability'] != s['box_probability'] or abs(float(first['threshold_margin'])-float(s['threshold_margin']))>1e-10:
            raise ValueError('研究用のBOX確率と購入判定閾値は初回から変更できません。')
    cols=list(s)
    con.execute(f"INSERT INTO snapshots ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})",[s[x] for x in cols])
    con.commit()


def settle(con: sqlite3.Connection,race_id: str,winning_combo: tuple[int,int,int],payoff100: int, note: str="") -> None:
    valid_race_id(race_id)
    if con.execute("SELECT 1 FROM results WHERE race_id=?",(race_id,)).fetchone():
        raise ValueError("このレースの結果は記録済みです。誤入力を上書きしない設計です。")
    if len(set(winning_combo))!=3 or not all(1<=v<=18 for v in winning_combo):
        raise ValueError("的中3頭の馬番が不正です。")
    if not isinstance(payoff100,int) or payoff100 < 0 or (payoff100==0 and not any(x in note for x in ('返還','不成立','中止'))):
        raise ValueError('通常レースは公式3連複100円払戻を正の整数で入力してください。')
    con.execute("INSERT INTO results VALUES (?,?,?,?,?)",(race_id,json.dumps(sorted(winning_combo)),payoff100,now_jst().isoformat(),note[:400]))
    con.commit()


def evaluate_history(con: sqlite3.Connection) -> pd.DataFrame:
    sn=pd.read_sql_query("SELECT * FROM snapshots ORDER BY race_id,captured_at_jst",con)
    if sn.empty: return pd.DataFrame()
    # A race's official analysis row is the latest independently verified snapshot at least 5m before start;
    # lacking that, the latest captured snapshot is only displayed for information, not ROI calculation.
    data=[]
    res={r['race_id']:r for r in con.execute("SELECT * FROM results")}
    for race,subset in sn.groupby('race_id',sort=False):
        eligible=subset[subset.timing_verified==1]
        qualified=not eligible.empty
        row=(eligible if qualified else subset).sort_values("captured_at_jst").iloc[-1]
        item={"race_id":race,"出走時刻":row.start_jst,"取得時刻":row.captured_at_jst,
              "選択4頭":row.selected4,"判定":row.decision,"判定理由":row.decision_reason,
              "オッズ4組":row.trio_odds4,"時刻検証済":qualified,
              "単勝順位":row.selected_rank,"確率出所":row.probability_source,
              "公式結果あり":race in res,"購入扱い":bool(qualified and str(row.decision).startswith('購入候補')),
              "特殊払戻・集計保留":False}
        if race in res:
            note=str(res[race]['note'] or '')
            if any(tag in note for tag in ('同着','返還','不成立','中止','取消','除外')):
                item['特殊払戻・集計保留']=True
            item['実際の的中3頭']=res[race]['winning_combo']
            win=set(json.loads(res[race]['winning_combo']))
            selected=set(json.loads(row.selected4))
            item['BOX的中']=win.issubset(selected)
            item['公式払戻円']=int(res[race]['official_payout_yen100']) if item['BOX的中'] else 0
            item['仮に全レース購入した回収率']=item['公式払戻円']/BOX_COST*100
            if item['特殊払戻・集計保留']:
                item['仮に全レース購入した回収率']=None
        else:
            item['実際の的中3頭']='';item['BOX的中']=None;item['公式払戻円']=None;item['仮に全レース購入した回収率']=None
        data.append(item)
    return pd.DataFrame(data)


def export_zip(con: sqlite3.Connection) -> bytes:
    info=pd.read_sql_query("SELECT * FROM snapshots ORDER BY captured_at_jst",con)
    result=pd.read_sql_query("SELECT * FROM results ORDER BY race_id",con)
    summary=evaluate_history(con)
    buff=io.BytesIO()
    with zipfile.ZipFile(buff,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("snapshots_all.csv",info.to_csv(index=False).encode('utf-8-sig'))
        z.writestr("results_all.csv",result.to_csv(index=False).encode('utf-8-sig'))
        z.writestr("evaluation_by_race.csv",summary.to_csv(index=False).encode('utf-8-sig'))
        z.writestr("READ_ME_FIRST.txt",(
          'JRA 3連複4頭BOX 取得時刻付き検証記録\n'
          'snapshots_all.csv: 追加のみ。保存時刻と市場の更新時刻を確認すること。\n'
          'results_all.csv: 開催後の確定払戻。\n'
          'evaluation_by_race.csv: 各レースの発走5分前以前の最新検証済み記録。\n'
          '発走前に取得したオッズは、締切確定オッズを保証しない。\n'
          '予測確率は外部入力で校正が未検証。購入候補=利益実証ではない。\n'
          'Streamlit Cloud はローカルDBが消える可能性があるのでZIPを必ず外部保存。\n'
        ).encode('utf-8'))
    return buff.getvalue()


def restore_zip(con: sqlite3.Connection, raw_zip: bytes) -> tuple[int,int]:
    """Restores without changing any existing recorded prediction or outcome."""
    with zipfile.ZipFile(io.BytesIO(raw_zip)) as z:
        names=set(z.namelist())
        if not {'snapshots_all.csv','results_all.csv'}.issubset(names):
            raise ValueError('このアプリが出力したバックアップZIPではありません。')
        sn=pd.read_csv(io.BytesIO(z.read('snapshots_all.csv')), encoding='utf-8-sig', dtype=str, keep_default_na=False)
        rs=pd.read_csv(io.BytesIO(z.read('results_all.csv')), encoding='utf-8-sig', dtype=str, keep_default_na=False)
    snap_cols=[r[1] for r in con.execute('PRAGMA table_info(snapshots)')]
    res_cols=[r[1] for r in con.execute('PRAGMA table_info(results)')]
    if not set(snap_cols).issubset(set(sn.columns)) or not set(res_cols).issubset(set(rs.columns)):
        raise ValueError('バックアップの列構成が現在のバージョンと一致しません。')
    sn=sn[snap_cols];rs=rs[res_cols]
    for _,row in sn.iterrows():
        if not re.fullmatch(r'[0-9a-f]{32}',row['snapshot_id']):
            raise ValueError('不正なsnapshot_idを検出したため復元を中止しました。')
        valid_race_id(row['race_id'])
        as_jst(row['captured_at_jst'])
        if len(json.loads(row['selected4']))!=4:
            raise ValueError('選択4頭のデータが不正です。')
    for _,row in rs.iterrows():
        valid_race_id(row['race_id'])
        if len(json.loads(row['winning_combo']))!=3:
            raise ValueError('的中3頭のデータが不正です。')
    # idempotent import, never overwrite. A backup cannot silently change an existing row.
    added_s=added_r=0
    with con:
        for _,row in sn.iterrows():
            rowdata=[None if row[c]=='' and c in {'box_probability','ev_lower_bound','ev_upper_bound','model_ev_reference'} else row[c] for c in snap_cols]
            cur=con.execute('INSERT OR IGNORE INTO snapshots ('+','.join(snap_cols)+') VALUES ('+','.join(['?']*len(snap_cols))+')', rowdata)
            added_s += cur.rowcount
        for _,row in rs.iterrows():
            vals=[row[c] for c in res_cols]
            cur=con.execute('INSERT OR IGNORE INTO results ('+','.join(res_cols)+') VALUES ('+','.join(['?']*len(res_cols))+')', vals)
            added_r += cur.rowcount
    return added_s,added_r


def show_app():
    import streamlit as st
    st.set_page_config(page_title="JRA 4頭BOX 購入・見送り研究",page_icon="🏇",layout="wide")
    st.title("🏇 3連複4頭BOX｜発走前オッズ保存と購入・見送り検証")
    st.caption("正式な利益モデルではなく、発走前データを蓄積する研究アプリです。オッズは取得後も変動します。")
    con=db_connect()
    with st.sidebar:
        st.header("研究プロトコル")
        st.write("固定：人気上位7頭から4頭を選択、4頭BOX・100円×4点。購入推奨数にノルマなし。")
        margin=RESEARCH_MARGIN
        st.caption("研究用安全余裕は15%で固定（過去成績を見て変更しません）。未校正の購入候補判定は信用できません。")
        st.warning("Streamlit Cloud のローカル保存は永続保証がありません。終了ごとにバックアップZIPを保存してください。")
    tab1,tab2,tab3=st.tabs(["① オッズ取得・判定・固定保存","② レース後の結果入力","③ 研究履歴・バックアップ"])
    with tab1:
        c1,c2,c3=st.columns([1.1,1,1])
        with c1: race_id=st.text_input("netkeibaレースID（12桁）",placeholder="2026XXXXXXXX",key="rid")
        with c2: race_date=st.date_input("開催日（JST）",value=now_jst().date())
        with c3: race_time=st.time_input("発走時刻（JST）",value=dt.time(15,0),step=dt.timedelta(minutes=5))
        selection_mode=st.radio("選択4頭の確定方法",["モデル予測CSVから人気上位7頭内の上位4頭","既存アプリで確定済みの4頭を直接指定"],horizontal=True)
        uploaded=None; manual_four='';model_version=st.text_input("4頭選択に使用したモデルのバージョン",value="既存モデル（ユーザー側で確認）")
        if selection_mode.startswith("モデル"):
            uploaded=st.file_uploader("予測CSV（馬番＋top3_probability または model_rank を含む）",type=['csv'],key='prediction')
        else:
            manual_four=st.text_input("4頭の馬番（例 1, 3, 7, 12）")
        with st.expander("BOX的中確率（任意・未校正の推定値は研究用途のみ）"):
            enable_prob=st.checkbox("このレースのBOX的中確率の推定値を入力する")
            p_percent=st.number_input("4頭BOX的中確率（％）",min_value=.01,max_value=100.,value=30.,step=.5,disabled=not enable_prob)
            st.caption("2026年のグループ平均32.41％を各レースの確率として転用することはできません。")
        source=st.radio("オッズ取得元",["netkeiba API（当日取得）","JSONファイルから読込（検証・取得失敗時の参考用）"],horizontal=True)
        doc1=doc7=None
        if source.startswith("JSON"):
            up1=st.file_uploader("単勝 type=1 のAPI応答JSON",type=['json'],key='json1')
            up7=st.file_uploader("3連複 type=7 のAPI応答JSON",type=['json'],key='json7')
            if up1 and up7:
                try: doc1=json.load(up1);doc7=json.load(up7)
                except Exception as e:st.error(f"JSONの読込失敗: {e}")
        st.write("ボタンを押した瞬間に取得・記録します。取得履歴は上書きせず追加保存します。")
        if st.button("🔄 単勝と3連複を一括取得して判定・保存",type="primary"):
            try:
                race_id=valid_race_id(race_id)
                start=dt.datetime.combine(race_date,race_time,tzinfo=JST)
                # Do not import results or later market data into this process.
                if source.startswith("netkeiba"):
                    doc1=fetch_netkeiba(race_id,1)
                    doc7=fetch_netkeiba(race_id,7)
                    source_code="netkeiba_api"
                else:
                    if doc1 is None or doc7 is None:
                        raise ValueError("単勝と3連複のJSONを両方アップロードしてください。")
                    source_code="unverified_json"
                wins=parse_odds_doc(doc1,1)
                ranks=win_popularity(wins)
                first=con.execute("SELECT selected4,model_digest,model_version,box_probability,combo_probs4,threshold_margin FROM snapshots WHERE race_id=? ORDER BY captured_at_jst LIMIT 1",(race_id,)).fetchone()
                digest=''
                if uploaded:
                    predictions,digest=read_prediction_file(uploaded.getvalue(),race_id)
                if first:
                    chosen4=tuple(json.loads(first['selected4']))
                    digest=first['model_digest'];model_version=first['model_version']
                elif selection_mode.startswith("モデル"):
                    if uploaded is None:raise ValueError("モデル予測CSVを添付してください。")
                    if set(predictions['馬番']) != set(wins):
                        raise ValueError('単勝オッズの馬番と予測CSVの全出走馬番が一致しません。取消・取得漏れを確認してください。')
                    chosen4=choose4_from_top7(predictions,ranks)
                else:
                    chosen4=parse_four_numbers(manual_four)
                auto_q4=None
                if uploaded and selection_mode.startswith('モデル') and not first and not enable_prob:
                    try:
                        auto_pbox,auto_q4=estimate_box_probability_unvalidated(predictions,chosen4)
                    except ValueError as e:
                        st.warning(f'組確率は計算不可：{e}（記録は続行します）')
                        auto_pbox=None
                else:
                    auto_pbox=None
                if first:
                    # Lock decision parameters as well as the horse set. Odds alone may change.
                    auto_pbox=first['box_probability']
                    auto_q4={combo_key(k):float(v) for k,v in json.loads(first['combo_probs4']).items()} if first['combo_probs4'] else None
                    if first['threshold_margin'] is not None:
                        margin=float(first['threshold_margin'])
                capture=now_jst()
                s=create_snapshot(race_id=race_id,start=start,capture=capture,
                   win_doc=doc1,trio_doc=doc7,chosen4=chosen4,source=source_code,
                   model_digest=digest,model_version=model_version,
                   p_box=(auto_pbox if first else (p_percent/100 if enable_prob else auto_pbox)),margin=margin,
                   combo_probs4=auto_q4)
                if not first and any(ranks[no]>7 for no in chosen4):
                    raise ValueError('初回選択は人気上位7頭に限定してください。')
                store_snapshot(con,s)
                st.success("固定保存しました。画面を更新しても過去の記録は変更されません。")
                st.subheader(s['decision'])
                st.write(s['decision_reason'])
                st.info(s['timing_note'])
                rows=[{"組合せ":k,"取得オッズ":v,"100円的中払戻の暫定額":int(round(100*v))} for k,v in json.loads(s['trio_odds4']).items()]
                st.dataframe(pd.DataFrame(rows),hide_index=True,use_container_width=True)
                if s['box_probability'] is not None:
                    shown_p=100*s['box_probability']
                    st.write(f"仮のBOX的中確率 {shown_p:.2f}% を前提とした期待回収率の理論範囲："
                      f"{s['ev_lower_bound']*100:.1f}% 〜 {s['ev_upper_bound']*100:.1f}% "
                      f"（実績検証前で購入根拠には使えません）")
                    st.write(f"今回のBOX的中率が {shown_p:.2f}% なら、損益分岐の的中時平均オッズは "
                             f"{required_odds_for_hit_probability(s['box_probability']):.2f} 倍です。")
                    if s['model_ev_reference'] is not None:
                        st.write(f"4組別の暫定確率を使ったEV：{s['model_ev_reference']*100:.1f}%（**未校正で購入根拠に使えません**）")
            except Exception as e:
                st.error(f"取得・保存できませんでした：{e}")
        st.caption('APIのtype=1（単勝）・type=7（3連複）を使用。取得成功は発走前に購入できる価格の保証ではありません。')
    with tab2:
        st.subheader("レース後に公式結果を追加する")
        outcome_race=st.text_input("結果を登録する12桁レースID",key='result_race')
        outcome_combo=st.text_input("3連複の的中馬番（例 2-5-8）",key='result_combo')
        outcome_payout=st.number_input("公式3連複払戻（100円あたり・円）",min_value=0,max_value=100000000,value=0,step=10)
        note=st.text_input("特記事項（同着・取消・返還などがあれば記入）")
        st.warning("同着・返還・不成立など特殊な結果は通常計算へ混ぜず、別処理が必要です。")
        if st.button("公式払戻を一度だけ記録"):
            try:
                numbers=combo_key(outcome_combo)
                if not con.execute("SELECT 1 FROM snapshots WHERE race_id=?",(outcome_race,)).fetchone():
                    raise ValueError("このレースの発走前スナップショットがありません。")
                if now_jst() <= as_jst(con.execute("SELECT start_jst FROM snapshots WHERE race_id=? LIMIT 1",(outcome_race,)).fetchone()[0]):
                    raise ValueError("発走時刻以前に結果は登録できません。")
                settle(con,outcome_race,numbers,int(outcome_payout),note)
                st.success("結果を保存しました。過去の購入判定は変更されません。")
            except Exception as e: st.error(str(e))
    with tab3:
        st.subheader("発走前の取得記録と、その後の実績")
        history=evaluate_history(con)
        if history.empty:st.info("記録がありません。")
        else:
            st.dataframe(history,hide_index=True,use_container_width=True)
            reviewed=history[history['公式結果あり']==True]
            valid=reviewed[(reviewed['時刻検証済']==True)&(reviewed['特殊払戻・集計保留']==False)]
            if not valid.empty:
                all_hit=int(valid['BOX的中'].sum())
                st.write(f"時刻確認済み・結果照合済み：{len(valid)}件／全購入仮定の的中={all_hit}件")
                for is_buy,name in [(True,"研究上の購入候補"),(False,"見送り・判定保留")]:
                    part=valid[valid['購入扱い']==is_buy]
                    if not part.empty:
                        p=int(part['公式払戻円'].sum());stake=len(part)*BOX_COST
                        st.write(f"{name}：{len(part)}レース、的中{int(part['BOX的中'].sum())}、"
                                 f"投資仮定{stake:,}円・払戻{p:,}円・回収率{100*p/stake:.2f}%")
        st.divider()
        st.download_button("⬇ 全履歴・全組オッズ・結果をZIP保存",data=export_zip(con),
            file_name=f"JRA_4BOX_snapshot_backup_{now_jst():%Y%m%d_%H%M%S}.zip",mime="application/zip")
        st.caption("必ず外部へ保存してください。クラウドのローカルDBの永続性は保証されません。")
        with st.expander('バックアップZIPを再読込（再デプロイ・端末移行時）'):
            backup=st.file_uploader('このアプリが出力したZIP',type=['zip'],key='importbackup')
            if st.button('バックアップを復元',disabled=backup is None):
                try:
                    sn,rs=restore_zip(con,backup.getvalue())
                    st.success(f'追記しました：スナップショット{sn}件・レース結果{rs}件。既存記録は上書きしていません。')
                except Exception as e:
                    st.error(f'復元できませんでした：{e}')
    con.close()


if __name__ == "__main__":
    show_app()
