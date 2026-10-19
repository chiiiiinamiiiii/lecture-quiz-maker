"""§11 の確認: Plotly の投げ縄で囲んだ点の id が Streamlit (Python側) で取れるか。

実行:  .venv/bin/streamlit run lasso_check.py
"""
import random

import pandas  # noqa: F401  plotly の遅延 import が Streamlit のスレッドと競合して落ちるのを防ぐ

import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="lasso check", layout="wide")
st.title("§11 投げ縄選択の確認")
st.caption("グラフ右上のツールバーで Lasso Select を選び、点を囲んでください。")

# ダミーの点 20 個（id は c00..c19、位置は毎回同じになるよう seed 固定）
rng = random.Random(0)
ids = [f"c{i:02d}" for i in range(20)]
xs = [rng.uniform(0, 10) for _ in ids]
ys = [rng.uniform(0, 10) for _ in ids]

fig = go.Figure(
    go.Scatter(
        x=xs,
        y=ys,
        mode="markers+text",
        text=ids,
        textposition="top center",
        marker=dict(size=14),
        customdata=ids,  # ← ここに入れた id が選択結果として返ってくる
        hovertemplate="%{customdata}<extra></extra>",
    )
)
fig.update_layout(dragmode="lasso", height=600)

event = st.plotly_chart(
    fig,
    on_select="rerun",          # 選択が変わるたびに再実行
    selection_mode=("lasso", "box"),
    key="map",
)

points = event.selection.points
selected_ids = [p["customdata"] for p in points]

st.subheader(f"選択された id（{len(selected_ids)} 個）")
st.write(selected_ids)

with st.expander("生の選択イベント"):
    st.json(event.selection)
