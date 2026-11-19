import hmac
import json
from io import StringIO

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import pyotp
import requests
import streamlit as st
import yfinance as yf
from plotly.subplots import make_subplots

st.set_page_config(
    page_title="Dividend Terminal",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

REPO_OWNER = "cnilsson27"
REPO_NAME = "dividend-bot"


# ================= SÄKERHET: LÖSENORD + MFA =================
def check_authentication():
    if st.session_state.get("authenticated", False):
        return True

    required_secrets = ["GITHUB_TOKEN", "APP_PASSWORD", "MFA_SECRET"]
    missing = [k for k in required_secrets if k not in st.secrets]
    if missing:
        st.error(f"Saknar följande nycklar i Streamlit Secrets: {', '.join(missing)}")
        st.stop()

    col_left, col_center, col_right = st.columns([1, 1.2, 1])
    with col_center:
        st.title("🔒 Dividend Terminal")
        st.caption("Skyddad vy — logga in med lösenord och 6-siffrig MFA-kod.")

        with st.form("login_form"):
            password = st.text_input("Lösenord", type="password")
            mfa_code = st.text_input("MFA-kod (6 siffror)", max_chars=6, placeholder="123456")
            submitted = st.form_submit_button("Logga in", use_container_width=True, type="primary")

        if submitted:
            totp = pyotp.TOTP(st.secrets["MFA_SECRET"].strip())
            pw_ok = hmac.compare_digest(password, st.secrets["APP_PASSWORD"])
            mfa_ok = totp.verify(mfa_code.strip(), valid_window=1)

            if pw_ok and mfa_ok:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Fel lösenord eller ogiltig MFA-kod.")

    st.stop()


check_authentication()


# ================= DATAHÄMTNING (GITHUB + LIVE) =================
def fetch_github_file(file_path):
    token = st.secrets["GITHUB_TOKEN"].strip()
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{file_path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.raw",
    }
    return requests.get(url, headers=headers, timeout=15)


@st.cache_data(ttl=300)
def load_trade_log():
    response = fetch_github_file("trade_log.csv")
    if response.status_code != 200 or not response.text.strip():
        return pd.DataFrame()
    try:
        df = pd.read_csv(StringIO(response.text))
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
    if not df.empty and "Datum" in df.columns:
        df["Datum"] = pd.to_datetime(df["Datum"])
    return df


@st.cache_data(ttl=300)
def load_portfolio():
    response = fetch_github_file("portfolio.json")
    if response.status_code != 200:
        return [], []
    try:
        data = json.loads(response.text)
    except json.JSONDecodeError:
        return [], []

    def extract_tickers(section):
        if isinstance(section, list):
            result = []
            for item in section:
                if isinstance(item, str):
                    result.append(item)
                elif isinstance(item, dict) and "ticker" in item:
                    result.append(item["ticker"])
            return result
        if isinstance(section, dict):
            return list(section.keys())
        return []

    portfolio_tickers = extract_tickers(data.get("portfolio", []))
    watchlist_tickers = extract_tickers(data.get("watchlist", []))
    return portfolio_tickers, watchlist_tickers


@st.cache_data(ttl=120)
def load_stock_history(ticker, period="1y"):
    df = yf.Ticker(ticker).history(period=period)
    if df.empty:
        return df

    df["SMA200"] = df["Close"].rolling(window=200).mean()
    df["SMA50"] = df["Close"].rolling(window=50).mean()

    delta = df["Close"].diff()
    gain = delta.clip(lower=0).rolling(window=14).mean()
    loss = (-delta.clip(upper=0)).rolling(window=14).mean()
    rs = gain / loss.replace(0, pd.NA)
    df["RSI14"] = 100 - (100 / (1 + rs))
    return df


@st.cache_data(ttl=120)
def build_live_snapshot(tickers):
    rows = []
    for t in tickers:
        hist = load_stock_history(t, period="1y")
        if hist.empty or len(hist) < 2:
            continue

        last_close = float(hist["Close"].iloc[-1])
        prev_close = float(hist["Close"].iloc[-2])
        day_chg_pct = ((last_close - prev_close) / prev_close) * 100

        sma200 = hist["SMA200"].iloc[-1]
        rsi14 = hist["RSI14"].iloc[-1]

        if pd.notna(sma200):
            dist_sma = ((last_close - sma200) / sma200) * 100
            status = "🟢 Över SMA200" if last_close >= sma200 else "🔴 Under SMA200"
        else:
            dist_sma = None
            status = "⚪ För lite data"

        rows.append({
            "Ticker": t,
            "Kurs ($)": round(last_close, 2),
            "Idag (%)": round(day_chg_pct, 2),
            "SMA 200 ($)": round(float(sma200), 2) if pd.notna(sma200) else None,
            "Avstånd SMA200 (%)": round(float(dist_sma), 2) if dist_sma is not None else None,
            "RSI (14)": round(float(rsi14), 1) if pd.notna(rsi14) else None,
            "Trendstatus": status,
        })
    return pd.DataFrame(rows)


def create_pro_chart(df, ticker):
    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.6, 0.2, 0.2],
        subplot_titles=(f"{ticker} — Kurs & Trendlinjer", "RSI (14)", "Volym"),
    )

    # 1. Candlestick + SMA 50 & SMA 200
    fig.add_trace(
        go.Candlestick(
            x=df.index,
            open=df["Open"],
            high=df["High"],
            low=df["Low"],
            close=df["Close"],
            name="Kurs",
            increasing_line_color="#00c853",
            decreasing_line_color="#ff5252",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(x=df.index, y=df["SMA50"], name="SMA 50", line=dict(color="#40c4ff", width=1.4)),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(x=df.index, y=df["SMA200"], name="SMA 200", line=dict(color="#ffab40", width=2)),
        row=1,
        col=1,
    )

    # 2. RSI 14
    fig.add_trace(
        go.Scatter(x=df.index, y=df["RSI14"], name="RSI 14", line=dict(color="#ce93d8", width=1.6)),
        row=2,
        col=1,
    )
    fig.add_hline(y=70, line_dash="dot", line_color="#ff5252", row=2, col=1)
    fig.add_hline(y=30, line_dash="dot", line_color="#00c853", row=2, col=1)

    # 3. Volym
    vol_colors = ["#00c853" if c >= o else "#ff5252" for c, o in zip(df["Close"], df["Open"])]
    fig.add_trace(
        go.Bar(x=df.index, y=df["Volume"], name="Volym", marker_color=vol_colors, opacity=0.6),
        row=3,
        col=1,
    )

    fig.update_layout(
        template="plotly_dark",
        height=720,
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        margin=dict(l=20, r=20, t=40, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    fig.update_yaxes(range=[0, 100], row=2, col=1)
    return fig


# ================= SIDOMENY =================
with st.sidebar:
    st.header("🎛️ Kontrollpanel")
    if st.button("🔄 Uppdatera live-data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")
    if st.button("🚪 Logga ut", use_container_width=True):
        st.session_state["authenticated"] = False
        st.rerun()


# ================= HUVUDVY =================
st.title("📈 Dividend Terminal — Live")

portfolio_tickers, watchlist_tickers = load_portfolio()
trade_df = load_trade_log()
snapshot_df = build_live_snapshot(portfolio_tickers) if portfolio_tickers else pd.DataFrame()

# Topp-KPI:er
kpi1, kpi2, kpi3, kpi4 = st.columns(4)
kpi1.metric("Innehav i portföljen", len(portfolio_tickers))

if not snapshot_df.empty:
    avg_day = snapshot_df["Idag (%)"].mean()
    above_sma = int((snapshot_df["Avstånd SMA200 (%)"] >= 0).sum())
    kpi2.metric("Snitt idag (portfölj)", f"{avg_day:+.2f}%", delta=f"{avg_day:+.2f}%")
    kpi3.metric("Trendstyrka (över SMA200)", f"{above_sma} / {len(snapshot_df)}")
else:
    kpi2.metric("Snitt idag (portfölj)", "—")
    kpi3.metric("Trendstyrka (över SMA200)", "—")

kpi4.metric("Loggade affärer", len(trade_df))

st.markdown("---")

tab_live, tab_port, tab_log = st.tabs([
    "📊 Live-grafer & Teknisk Analys",
    "💼 Portfölj & Bevakning",
    "📜 Transaktionslogg",
])

with tab_live:
    all_tickers = portfolio_tickers + [t for t in watchlist_tickers if t not in portfolio_tickers]
    c1, c2, c3 = st.columns([2, 1.5, 1.5])

    with c1:
        selected_ticker = st.selectbox(
            "Välj aktie från portfölj / bevakning",
            options=all_tickers if all_tickers else ["O", "MAIN", "STAG", "PEP"],
        )
    with c2:
        custom_ticker = st.text_input("Eller skriv valfri ticker (t.ex. MSFT)").strip().upper()
    with c3:
        period = st.selectbox("Tidsperiod", options=["6mo", "1y", "2y", "5y"], index=1)

    active_ticker = custom_ticker if custom_ticker else selected_ticker
    hist_df = load_stock_history(active_ticker, period=period)

    if not hist_df.empty:
        latest = hist_df["Close"].iloc[-1]
        prev = hist_df["Close"].iloc[-2] if len(hist_df) > 1 else latest
        diff_pct = ((latest - prev) / prev) * 100
        m1, m2, m3 = st.columns(3)
        m1.metric(f"{active_ticker} Senaste kurs", f"${latest:.2f}", f"{diff_pct:+.2f}%")
        if pd.notna(hist_df["SMA200"].iloc[-1]):
            m2.metric("SMA 200", f"${hist_df['SMA200'].iloc[-1]:.2f}")
        if pd.notna(hist_df["RSI14"].iloc[-1]):
            m3.metric("RSI (14)", f"{hist_df['RSI14'].iloc[-1]:.1f}")

        st.plotly_chart(create_pro_chart(hist_df, active_ticker), use_container_width=True)
    else:
        st.warning(f"Kunde inte hämta kursdata för {active_ticker}.")

with tab_port:
    st.subheader("Live-översikt: Aktuella innehav")
    if not snapshot_df.empty:
        st.dataframe(snapshot_df, use_container_width=True, hide_index=True)

        fig_bar = px.bar(
            snapshot_df,
            x="Ticker",
            y="Avstånd SMA200 (%)",
            color="Avstånd SMA200 (%)",
            color_continuous_scale=["#ff5252", "#ffd740", "#00c853"],
            template="plotly_dark",
            title="Marginal till säljsignal (avstånd till SMA 200 i %)",
        )
        fig_bar.add_hline(y=0, line_dash="dash", line_color="white")
        st.plotly_chart(fig_bar, use_container_width=True)
    else:
        st.info("Hittade inga aktier i `portfolio.json` ännu.")

    if watchlist_tickers:
        st.subheader("Bevakningslista (Watchlist)")
        watch_df = build_live_snapshot(watchlist_tickers)
        st.dataframe(watch_df, use_container_width=True, hide_index=True)

with tab_log:
    st.subheader("Historiska signaler (trade_log.csv)")
    if not trade_df.empty:
        activity = trade_df.groupby(["Datum", "Aktion"]).size().reset_index(name="Antal")
        fig_act = px.bar(
            activity,
            x="Datum",
            y="Antal",
            color="Aktion",
            color_discrete_map={"KÖP": "#00c853", "SÄLJ": "#ff5252"},
            barmode="group",
            template="plotly_dark",
        )
        st.plotly_chart(fig_act, use_container_width=True)
        st.dataframe(trade_df.sort_values(by="Datum", ascending=False), use_container_width=True, hide_index=True)
    else:
        st.info("Inga loggade affärer i `trade_log.csv` ännu.")
