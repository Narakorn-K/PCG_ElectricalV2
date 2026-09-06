"""
Streamlit dashboard: สถานะไฟฟ้า Air Compressor
แท็บ 1: Real-time monitor จาก Google Sheet ที่ Node-RED POST เข้าไป (ผ่าน Google Apps Script)
แท็บ 2: ปริมาณการใช้ไฟฟ้ารายวัน (On Peak / Off Peak / Total) จากชีต "Daily"
         + เทียบกับยอดผลิต Extruder (PD Ton) จากชีต "Product Ton" แบบกราฟรวม (combo chart)

วิธีติดตั้ง:
    pip install streamlit pandas requests streamlit-autorefresh altair

วิธีรัน:
    streamlit run meter_dashboard.py
"""

import re
import streamlit as st
import pandas as pd
import altair as alt
import requests
from io import StringIO
from datetime import datetime, timedelta
from streamlit_autorefresh import st_autorefresh

# ============== CONFIG: แท็บ 1 (Real-time) ==============
# หา SHEET_ID จาก URL ของ Google Sheet:
# https://docs.google.com/spreadsheets/d/[SHEET_ID]/edit#gid=[GID]
SHEET_ID = "1gFKOoTb9XnarHqawBima7fuDb82yg5KXFqL15f_plpw"
GID = "0"  # เลข gid ของแท็บ (ดูจากท้าย URL หลัง #gid=)

COL_TIME = "Timestamp"
SHEET_COLS = {  # ชื่อคอลัมน์จริงในชีต real-time (ห้ามแก้)
    "ac1_3": "AC1-3",
    "ac4_6": "AC4-6",
    "ac7": "AC7",
    "ac8": "AC8",
}
DISPLAY_NAMES = {  # ชื่อที่อยากให้แสดงบนจอ (แก้ได้อิสระ)
    "ac1_3": "AC1-3 (Production)",
    "ac4_6": "AC4-6 (Production)",
    "ac7": "AC7 (Packing)",
    "ac8": "AC8 (Packing)",
}
RUN_STOP_THRESHOLD = 20  # >= ค่านี้ = Run, ต่ำกว่า = Stop
REFRESH_SEC = 60
TZ_OFFSET_HOURS = 7  # ชีตเป็นเวลาไทย (UTC+7) แต่ server รันเป็น UTC

# ============== CONFIG: แท็บ 2 (Daily Usage) ==============
DAILY_SHEET_ID = "1Ym2yfzkLTyLTtJtLZSSgWoeew_IPWUaI_u6d45jKUnw"
DAILY_GID = "0"
DAILY_SHEET_NAME = "Daily"

# ชื่อ Meter ตามคอลัมน์ A ในชีต Daily -> map ไปยัง key เดียวกับแท็บ 1 (ใช้ DISPLAY_NAMES ร่วมกัน)
DAILY_METER_NAME_MAP = {
    "MCC5_6": "ac4_6",
    "AirComp_P7": "ac7",
    "AirComp_P8": "ac8",
    "AirComp_P1234": "ac1_3",
}
DAILY_DATA_YEAR = 2026  # ปี ค.ศ. ของข้อมูล (ในชีตมีแค่ dd/mm ไม่มีปี)

# กลุ่มปั๊มลม: Process = AC1-3 + AC4-6 รวมกัน, Packing = AC7 + AC8 รวมกัน
METER_GROUP_MAP = {
    "ac1_3": "ปั๊มลม Process",
    "ac4_6": "ปั๊มลม Process",
    "ac7": "ปั๊มลม Packing",
    "ac8": "ปั๊มลม Packing",
}
GROUP_ORDER = ["ปั๊มลม Process", "ปั๊มลม Packing"]
GROUP_COLORS = {"ปั๊มลม Process": "#E58426", "ปั๊มลม Packing": "#2E6B34"}

# ============== CONFIG: ยอดผลิต Extruder (PD Ton) ==============
# อยู่ในไฟล์เดียวกับชีต Daily แต่คนละแท็บ ชื่อ "Product Ton"
# คอลัมน์ A = วันที่ (รูปแบบ dd-mm-yyyy), คอลัมน์ B = ยอดผลิต (ตัน)
PRODUCT_TON_GID = "1847351361"
PRODUCT_TON_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{DAILY_SHEET_ID}/export?format=csv&gid={PRODUCT_TON_GID}"
)

# วันที่ PD Ton ต่ำกว่าค่านี้ จะถือว่าเป็นวันหยุด/ไม่มีการผลิต -> ไฮไลต์พื้นหลังสีแดงอ่อนใต้แกน X
LOW_PRODUCTION_THRESHOLD = 50

# ==========================================================

CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={GID}"
DAILY_CSV_URL = f"https://docs.google.com/spreadsheets/d/{DAILY_SHEET_ID}/export?format=csv&gid={DAILY_GID}"

st.set_page_config(page_title="AC Compressor Power Monitor", page_icon="⚡", layout="wide")
st_autorefresh(interval=REFRESH_SEC * 1000, key="refresh")

# ============== ปรับขนาดฟอนต์ตรงนี้ ==============
TITLE_FONT_SIZE = "2.2rem"
METRIC_LABEL_SIZE = "1.1rem"
METRIC_VALUE_SIZE = "2.8rem"
METRIC_DELTA_SIZE = "1rem"
CHART_LEGEND_SIZE = 14
CHART_AXIS_SIZE = 12
PAGE_TOP_PADDING = "1.5rem"  # ระยะห่างจากขอบบนสุดของหน้า ลดตัวเลขให้ชิดขึ้น

st.markdown(f"""
<style>
h1 {{ font-size: {TITLE_FONT_SIZE} !important; }}
div[data-testid="stMetricLabel"] p {{ font-size: {METRIC_LABEL_SIZE} !important; }}
div[data-testid="stMetricValue"] {{ font-size: {METRIC_VALUE_SIZE} !important; }}
div[data-testid="stMetricDelta"] {{ font-size: {METRIC_DELTA_SIZE} !important; }}
div.block-container {{ padding-top: {PAGE_TOP_PADDING} !important; }}
</style>
""", unsafe_allow_html=True)
# ====================================================


@st.cache_data(ttl=REFRESH_SEC)
def load_realtime_data():
    resp = requests.get(CSV_URL, timeout=10)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    df = pd.read_csv(StringIO(resp.text))
    df[COL_TIME] = pd.to_datetime(df[COL_TIME], errors="coerce", dayfirst=True)
    df = df.dropna(subset=[COL_TIME]).sort_values(COL_TIME)
    return df


def get_status(value):
    if value >= RUN_STOP_THRESHOLD:
        return "Run", "normal"
    else:
        return "Stop", "off"


def _parse_number(x):
    if pd.isna(x):
        return 0.0
    s = str(x).replace(",", "").strip()
    if s == "":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


@st.cache_data(ttl=300)
def load_daily_data():
    resp = requests.get(DAILY_CSV_URL, timeout=15)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    raw = pd.read_csv(StringIO(resp.text), header=None)

    date_row = raw.iloc[0]
    n_cols = raw.shape[1]

    # หาบล็อกวัน (Onpeak/Offpeak/Total) เริ่มจากคอลัมน์ E (index 4) ทีละ 3 คอลัมน์
    day_blocks = []  # (onpeak_idx, offpeak_idx, total_idx, date, weekday_th)
    idx = 4
    while idx + 2 < n_cols:
        label = str(date_row[idx])
        m = re.search(r"(\d{2})/(\d{2})\s*\(([^)]+)\)", label)
        if m:
            dd, mm, wd = m.group(1), m.group(2), m.group(3)
            try:
                date_val = datetime(DAILY_DATA_YEAR, int(mm), int(dd))
            except ValueError:
                idx += 3
                continue
            day_blocks.append((idx, idx + 1, idx + 2, date_val, wd))
        idx += 3

    records = []
    for _, row in raw.iterrows():
        meter_name = str(row[0]).strip()
        if meter_name not in DAILY_METER_NAME_MAP:
            continue
        key = DAILY_METER_NAME_MAP[meter_name]
        for onpeak_idx, offpeak_idx, total_idx, date_val, wd in day_blocks:
            records.append({
                "meter_key": key,
                "meter_name": DISPLAY_NAMES.get(key, key),
                "group": METER_GROUP_MAP.get(key, key),
                "date": date_val,
                "weekday": wd,
                "on_peak": _parse_number(row[onpeak_idx]),
                "off_peak": _parse_number(row[offpeak_idx]),
                "total": _parse_number(row[total_idx]),
            })

    long_df = pd.DataFrame(records)
    long_df = long_df.sort_values(["date", "meter_key"])
    return long_df


@st.cache_data(ttl=300)
def load_product_ton_data():
    """โหลดยอดผลิต Extruder (PD Ton) จากชีต 'Product Ton' (คอลัมน์ A = วันที่, B = ตัน)
    รองรับหลายรูปแบบวันที่ เพราะตอน export เป็น CSV รูปแบบวันที่จาก Google Sheets
    อาจไม่ตรงกับที่เห็นในหน้าจอ (เช่น ถูก export เป็น yyyy-mm-dd แทนที่จะเป็น dd-mm-yyyy)
    """
    resp = requests.get(PRODUCT_TON_CSV_URL, timeout=15)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    raw = pd.read_csv(StringIO(resp.text), header=None, names=["date_raw", "pd_ton"])

    raw["date_raw"] = raw["date_raw"].astype(str).str.strip()
    parsed = pd.Series(pd.NaT, index=raw.index, dtype="datetime64[ns]")

    # ลองไล่ทีละรูปแบบที่เป็นไปได้ ก่อนจะปล่อยให้ pandas เดาแบบอิสระ (dayfirst) เป็นด่านสุดท้าย
    candidate_formats = ["%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y", "%d-%m-%y", "%d/%m/%y"]
    for fmt in candidate_formats:
        still_missing = parsed.isna()
        if not still_missing.any():
            break
        parsed.loc[still_missing] = pd.to_datetime(
            raw.loc[still_missing, "date_raw"], format=fmt, errors="coerce"
        )

    still_missing = parsed.isna()
    if still_missing.any():
        parsed.loc[still_missing] = pd.to_datetime(
            raw.loc[still_missing, "date_raw"], dayfirst=True, errors="coerce"
        )

    raw["date"] = parsed
    raw["pd_ton"] = raw["pd_ton"].apply(_parse_number)
    n_total = len(raw)
    n_parsed = raw["date"].notna().sum()
    result = raw.dropna(subset=["date"])[["date", "pd_ton"]].copy()
    return result, n_total, n_parsed, raw.head(5)


st.title("⚡ สถานะการใช้ไฟฟ้า Air Compressor")

tab1, tab2 = st.tabs(["📡 Real-time Monitor", "📊 การใช้ไฟฟ้ารายวัน"])

# ================= แท็บ 1: Real-time =================
with tab1:
    try:
        df = load_realtime_data()
        if df.empty:
            st.warning("ยังไม่มีข้อมูลใน Google Sheet")
        else:
            latest = df.iloc[-1]
            last_time = latest[COL_TIME]
            now_th = datetime.utcnow() + timedelta(hours=TZ_OFFSET_HOURS)
            age_sec = (now_th - last_time.to_pydatetime().replace(tzinfo=None)).total_seconds()

            st.caption(f"อัปเดตล่าสุด: {last_time.strftime('%Y-%m-%d %H:%M:%S')} ({int(age_sec)} วินาทีที่แล้ว)")
            if age_sec > REFRESH_SEC * 4:
                st.error("⚠️ ไม่มีข้อมูลใหม่เข้ามานานผิดปกติ ตรวจสอบการเชื่อมต่อ Node-RED")

            cols = st.columns(4)
            for col, key in zip(cols, SHEET_COLS.keys()):
                value = float(latest[SHEET_COLS[key]])
                status, color = get_status(value)
                col.metric(DISPLAY_NAMES[key], f"{value:.2f} kW", delta=status, delta_color=color)

            total_kw = sum(float(latest[c]) for c in SHEET_COLS.values())
            st.metric("รวมทั้งหมด (Total)", f"{total_kw:.2f} kW")

            st.subheader("แนวโน้มย้อนหลัง")
            chart_df = df.tail(120)[[COL_TIME] + list(SHEET_COLS.values())]
            chart_df = chart_df.rename(columns={SHEET_COLS[k]: DISPLAY_NAMES[k] for k in SHEET_COLS.keys()})
            chart_long = chart_df.melt(id_vars=COL_TIME, var_name="กลุ่ม", value_name="kW")

            line_chart = (
                alt.Chart(chart_long)
                .mark_line()
                .encode(
                    x=alt.X(f"{COL_TIME}:T", title=None, axis=alt.Axis(labelFontSize=CHART_AXIS_SIZE, titleFontSize=CHART_AXIS_SIZE)),
                    y=alt.Y("kW:Q", axis=alt.Axis(labelFontSize=CHART_AXIS_SIZE, titleFontSize=CHART_AXIS_SIZE)),
                    color=alt.Color("กลุ่ม:N", legend=alt.Legend(title=None, labelFontSize=CHART_LEGEND_SIZE, symbolStrokeWidth=3)),
                )
                .properties(height=350)
            )
            st.altair_chart(line_chart, use_container_width=True)

            with st.expander("ดูข้อมูลดิบล่าสุด 20 แถว"):
                st.dataframe(df.tail(20).sort_values(COL_TIME, ascending=False), use_container_width=True)

    except Exception as e:
        st.error(f"โหลดข้อมูลไม่สำเร็จ: {e}")
        st.info("ตรวจสอบว่า SHEET_ID / GID ถูกต้อง และชีตเปิด public (Anyone with link can view)")

# ================= แท็บ 2: Daily Usage =================
with tab2:
    try:
        daily_df = load_daily_data()
        ton_df, ton_n_total, ton_n_parsed, ton_sample = load_product_ton_data()

        if daily_df.empty:
            st.warning("ไม่พบข้อมูลในชีต Daily ตรวจสอบชื่อ Meter / SHEET_ID / GID")
        else:
            month_options = sorted(daily_df["date"].dt.to_period("M").unique())
            month_labels = {p: p.strftime("%B %Y") for p in month_options}
            selected_month = st.selectbox(
                "เลือกเดือน",
                options=month_options,
                format_func=lambda p: month_labels[p],
                index=len(month_options) - 1,
            )

            filtered = daily_df[daily_df["date"].dt.to_period("M") == selected_month].copy()
            filtered["day_label"] = filtered["date"].dt.strftime("%d/%m") + " (" + filtered["weekday"] + ")"

            # --- สรุปยอดใช้ไฟฟ้าเป็นการ์ด (แยกตามกลุ่ม Process / Packing) ---
            st.markdown(f"**สรุปยอดใช้ไฟฟ้าเดือน {month_labels[selected_month]}**")
            group_summary = filtered.groupby("group", sort=False)[["on_peak", "off_peak", "total"]].sum()
            group_summary = group_summary.reindex(GROUP_ORDER).fillna(0)

            card_cols = st.columns(len(GROUP_ORDER) + 1)
            for card_col, group_name in zip(card_cols, GROUP_ORDER):
                row = group_summary.loc[group_name]
                card_col.metric(
                    group_name,
                    f"{row['total']:,.0f} kWh",
                    delta=f"On Peak {row['on_peak']:,.0f} | Off Peak {row['off_peak']:,.0f}",
                    delta_color="off",
                )
            card_cols[-1].metric("รวมทั้งหมด (Total เดือนนี้)", f"{group_summary['total'].sum():,.0f} kWh")

            # --- เตรียมข้อมูลกราฟแท่ง: รวม AC1-3+AC4-6 = Process, AC7+AC8 = Packing (ยอดรวม kWh ต่อวัน ไม่แยก peak) ---
            bar_long = (
                filtered.groupby(["date", "day_label", "weekday", "group"], sort=False)["total"]
                .sum()
                .reset_index()
                .rename(columns={"total": "kwh"})
            )
            bar_long["kwh"] = bar_long["kwh"].fillna(0)

            # --- รวมยอดผลิต PD Ton เข้ากับช่วงเดือนที่เลือก ---
            ton_filtered = ton_df[ton_df["date"].dt.to_period("M") == selected_month].copy()
            ton_filtered = ton_filtered.merge(
                filtered[["date", "day_label"]].drop_duplicates(), on="date", how="left"
            )
            ton_filtered = ton_filtered.dropna(subset=["day_label"])
            ton_filtered["pd_ton"] = ton_filtered["pd_ton"].fillna(0)

            if ton_filtered.empty:
                st.warning(
                    f"⚠️ ไม่พบข้อมูล PD Ton สำหรับเดือน {month_labels[selected_month]} "
                    f"(อ่านข้อมูลจากชีต Product Ton ได้ทั้งหมด {ton_n_total} แถว แปลงวันที่สำเร็จ {ton_n_parsed} แถว) "
                    "ลองเปิดช่อง 'ตรวจสอบข้อมูล PD Ton' ด้านล่างเพื่อดูตัวอย่างข้อมูลดิบ"
                )

            # ลำดับวันบนแกน X ให้ตรงกับข้อมูลไฟฟ้า
            day_order = filtered.sort_values("date")["day_label"].unique().tolist()

            # วันที่ผลิตต่ำกว่า threshold -> ไฮไลต์พื้นหลังใต้แกน X (สมมติว่าคือวันหยุด/ไม่มีการผลิต)
            low_prod_days = ton_filtered.loc[ton_filtered["pd_ton"] < LOW_PRODUCTION_THRESHOLD, "day_label"].tolist()
            highlight_df = pd.DataFrame({"day_label": low_prod_days})

            def _safe_max(value, fallback=1.0):
                if value is None or pd.isna(value) or value <= 0:
                    return fallback
                return float(value)

            max_kwh = _safe_max(bar_long["kwh"].max()) * 1.15
            max_ton = _safe_max(ton_filtered["pd_ton"].max()) * 1.15

            base_x = alt.X(
                "day_label:N",
                sort=day_order,
                title=None,
                axis=alt.Axis(labelFontSize=CHART_AXIS_SIZE, titleFontSize=CHART_AXIS_SIZE, labelAngle=-45),
            )

            # แถบพื้นหลังไฮไลต์วันหยุด/วันที่ไม่มีการผลิต
            highlight_layer = (
                alt.Chart(highlight_df)
                .mark_rect(color="#FDE2E2", opacity=0.9)
                .encode(x=base_x)
                .properties(height=420)
            )

            bar_chart = (
                alt.Chart(bar_long)
                .mark_bar()
                .encode(
                    x=base_x,
                    xOffset=alt.XOffset("group:N", sort=GROUP_ORDER),
                    y=alt.Y(
                        "kwh:Q",
                        title="kWh",
                        scale=alt.Scale(domain=[0, max_kwh]),
                        axis=alt.Axis(labelFontSize=CHART_AXIS_SIZE, titleFontSize=CHART_AXIS_SIZE),
                    ),
                    color=alt.Color(
                        "group:N",
                        sort=GROUP_ORDER,
                        scale=alt.Scale(domain=GROUP_ORDER, range=[GROUP_COLORS[g] for g in GROUP_ORDER]),
                        legend=alt.Legend(title=None, labelFontSize=CHART_LEGEND_SIZE),
                    ),
                    tooltip=["day_label", "group", "kwh"],
                )
                .properties(height=420)
            )

            line_chart = (
                alt.Chart(ton_filtered)
                .mark_line(color="#D32F2F", point=alt.OverlayMarkDef(color="#D32F2F", shape="diamond", size=90))
                .encode(
                    x=base_x,
                    y=alt.Y(
                        "pd_ton:Q",
                        title="PD Ton",
                        scale=alt.Scale(domain=[0, max_ton]),
                        axis=alt.Axis(labelFontSize=CHART_AXIS_SIZE, titleFontSize=CHART_AXIS_SIZE),
                    ),
                    tooltip=["day_label", "pd_ton"],
                )
                .properties(height=420)
            )

            combo_chart = (
                alt.layer(highlight_layer, bar_chart, line_chart)
                .resolve_scale(y="independent")
                .properties(title="การใช้พลังงานปั๊มลม เทียบกับยอดการผลิต Extruder")
            )

            st.altair_chart(combo_chart, use_container_width=True)

            with st.expander("ดูข้อมูลดิบ"):
                st.dataframe(
                    filtered[["date", "weekday", "meter_name", "on_peak", "off_peak", "total"]]
                    .sort_values(["date", "meter_name"]),
                    use_container_width=True,
                )
                st.dataframe(ton_filtered.sort_values("date"), use_container_width=True)

            with st.expander("🔍 ตรวจสอบข้อมูล PD Ton (สำหรับ debug)"):
                st.write(f"อ่านจากชีต Product Ton ได้ทั้งหมด {ton_n_total} แถว, แปลงวันที่สำเร็จ {ton_n_parsed} แถว")
                st.write("ตัวอย่าง 5 แถวแรกที่อ่านได้ (คอลัมน์ date_raw คือค่าดิบจาก CSV, date คือค่าที่แปลงแล้ว):")
                st.dataframe(ton_sample, use_container_width=True)

    except Exception as e:
        st.error(f"โหลดข้อมูล Daily ไม่สำเร็จ: {e}")
        st.info("ตรวจสอบว่า DAILY_SHEET_ID / DAILY_GID / PRODUCT_TON_GID ถูกต้อง และชีตเปิด public (Anyone with link can view)")
