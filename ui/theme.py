"""色・記号・Plotly の設定。画面の見た目に関わる定数はここに集める。"""

OTHER_LABEL = "その他"
ALL_LECTURES = "all"
MIDPOINT_TRACE = "関係（辺の中点）"

# ---- 配色。背景・枠・辺はピンク系。点の 2 状態は「出題済み / 未出題」という情報なので、
#      ピンク背景の上で明度と彩度の両方がはっきり違う 2 色にする（濃淡だけの区別はしない）
PINK = "#D6336C"
BG = "#FFF5F8"
PLOT_BG = "#FFFBFC"                        # グラフ領域は周囲より少し明るくして範囲が分かるようにする
FRAME = "rgba(214,51,108,0.55)"            # グラフ領域の枠線
COLOR_UNASKED = "#2E1A47"   # 未出題: 暗い紫（低明度・低彩度）
COLOR_ASKED = "#00A3A3"     # 出題済み: 明るいティール（高明度・高彩度、ピンクの補色側）
COLOR_EDGE = "rgba(190,30,90,0.6)"
COLOR_EDGE_MID = "#C2185B"
COLOR_TEXT = "#3B1F2B"
SYMBOLS = ["circle", "square", "diamond", "triangle-up", "star", "hexagon", "cross",
           "pentagon", "triangle-down", "x"]

# ---- Plotly のツールバー: 矩形・投げ縄だけ残す。ズームは 2 本指、移動はスペース＋2 本指（PAN_JS）
PLOTLY_CONFIG = {
    "displayModeBar": True,
    "displaylogo": False,
    "scrollZoom": True,
    "doubleClick": "reset",
    "modeBarButtonsToRemove": [
        "zoom2d", "pan2d", "zoomIn2d", "zoomOut2d", "autoScale2d", "resetScale2d",
        "toImage", "hoverClosestCartesian", "hoverCompareCartesian", "toggleSpikelines",
    ],
}

