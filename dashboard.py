from datetime import date, datetime, timezone
from io import StringIO
import json
import time
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pyotp
import requests
import streamlit as st
import yfinance as yf

st.set_page_config(
    page_title="Dividend Terminal",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

REPO_OWNER = "cnilsson27"
REPO_NAME = "dividend-bot"


def get_stockholm_time_str():
    try:
        return datetime.now(ZoneInfo("Europe/Stockholm")).strftime("%H:%M:%S")
    except Exception:
        return datetime.now().strftime("%H:%M:%S")


# ================= SÄKERHET: ENBART MFA =================
def check_authentication():
    if st.session_state.get("authenticated", False):
        return True

    required_secrets = ["GITHUB_TOKEN", "MFA_SECRET"]
    missing = [k for k in required_secrets if k not in st.secrets]
    if missing:
        st.error(f"Saknar följande nycklar i Streamlit Secrets: {', '.join(missing)}")
        st.stop()

    if "failed_attempts" not in st.session_state:
        st.session_state["failed_attempts"] = 0

    _, col_center, _ = st.columns([1, 1.2, 1])
    with col_center:
        st.title("🔒 Dividend Terminal")
        st.caption("Ange den 6-siffriga koden från din autentiseringsapp.")

        if st.session_state["failed_attempts"] >= 5:
            st.error("För många felaktiga försök. Ladda om sidan för att försöka igen.")
            st.stop()

        with st.form("login_form"):
            mfa_code = st.text_input(
                "MFA-kod (6 siffror)",
                max_chars=6,
                placeholder="123456",
            )
            submitted = st.form_submit_button(
                "Logga in", use_container_width=True, type="primary"
            )

        if submitted:
            totp = pyotp.TOTP(st.secrets["MFA_SECRET"].strip())
            if totp.verify(mfa_code.strip(), valid_window=1):
                st.session_state["authenticated"] = True
                st.session_state["failed_attempts"] = 0
                st.rerun()
            else:
                st.session_state["failed_attempts"] += 1
                time.sleep(2)
                st.error("Ogiltig MFA-kod.")

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


@st.cache_data(ttl=45)
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


@st.cache_data(ttl=45)
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


def _to_date_obj(val):
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, (int, float)):
        try:
            return datetime.fromtimestamp(val, tz=timezone.utc).date()
        except Exception:
            return None
    if isinstance(val, str):
        try:
            return pd.to_datetime(val).date()
        except Exception:
            return None
    return None


@st.cache_data(ttl=3600)
def load_dividend_calendar(tickers):
    summary_rows = []
    payout_rows = []
    today = date.today()
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=365)

    for t in tickers:
        try:
            tk = yf.Ticker(t)
            info = tk.info or {}
            divs = tk.dividends
        except Exception:
            continue

        # Historiska utdelningar senaste 12 månaderna
        recent_divs = pd.Series(dtype=float)
        if divs is not None and not divs.empty:
            div_copy = divs.copy()
            if div_copy.index.tz is not None:
                div_copy.index = div_copy.index.tz_localize(None)
            recent_divs = div_copy[div_copy.index >= cutoff]

            for dt_idx, amount in recent_divs.items():
                payout_rows.append({
                    "Ticker": t,
                    "Datum": dt_idx.date(),
                    "Månad_nr": dt_idx.month,
                    "Månad": dt_idx.strftime("%b"),
                    "Utdelning ($)": round(float(amount), 4),
                })

        # Utdelningsfrekvens
        count_12m = len(recent_divs)
        if count_12m >= 10:
            freq = "Månadsvis"
        elif count_12m >= 3:
            freq = "Kvartalsvis"
        elif count_12m == 2:
            freq = "Halvårsvis"
        elif count_12m == 1:
            freq = "Årsvis"
        else:
            freq = "Okänd"

        # Hämta X-dag och utdelningsdag från calendar / info
        ex_date = None
        pay_date = None
        try:
            cal = tk.calendar
            if isinstance(cal, dict):
                ex_date = _to_date_obj(cal.get("Ex-Dividend Date"))
                pay_date = _to_date_obj(cal.get("Dividend Date"))
        except Exception:
            pass

        if ex_date is None:
            ex_date = _to_date_obj(info.get("exDividendDate"))

        # Beräkna årlig utdelning och direktavkastning säkert
        price = (
            info.get("currentPrice")
            or info.get("regularMarketPrice")
            or info.get("previousClose")
        )
        annual_div = info.get("dividendRate")
        if annual_div is None and not recent_divs.empty:
            annual_div = float(recent_divs.sum())

        if annual_div and price:
            div_yield = (float(annual_div) / float(price)) * 100
        else:
            raw_yield = info.get("dividendYield")
            if raw_yield is not None:
                div_yield = float(raw_yield) * 100 if float(raw_yield) < 1 else float(raw_yield)
            else:
                div_yield = None

        last_div = float(recent_divs.iloc[-1]) if not recent_divs.empty else info.get("lastDividendValue")

        days_to_ex = None
        ex_status = "—"
        if ex_date is not None:
            diff_days = (ex_date - today).days
            days_to_ex = diff_days
            if diff_days > 0:
                ex_status = f"Om {diff_days} dagar"
            elif diff_days == 0:
                ex_status = "Idag!"
            else:
                ex_status = f"För {abs(diff_days)} dagar sedan"

        summary_rows.append({
            "Ticker": t,
            "Bolag": info.get("shortName", t),
            "Frekvens": freq,
            "Årlig utdelning ($)": round(float(annual_div), 2) if annual_div is not None else None,
            "Direktavkastning (%)": round(float(div_yield), 2) if div_yield is not None else None,
            "Senaste utbetalning ($)": round(float(last_div), 3) if last_div is not None else None,
            "X-dag": ex_date.strftime("%Y-%m-%d") if ex_date else "Saknas",
            "Utdelningsdag": pay_date.strftime("%Y-%m-%d") if pay_date else "—",
            "Status X-dag": ex_status,
            "_sort_days": days_to_ex if (days_to_ex is not None and days_to_ex >= 0) else 9999,
        })

    cal_df = pd.DataFrame(summary_rows)
    if not cal_df.empty:
        cal_df = cal_df.sort_values(by="_sort_days", ascending=True).reset_index(drop=True)

    payout_df = pd.DataFrame(payout_rows)
    return cal_df, payout_df


def create_pro_chart(df, ticker):
    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.6, 0.2, 0.2],
        subplot_titles=(f"{ticker} — Kurs & Trendlinjer", "RSI (14)", "Volym"),
    )

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

    fig.add_trace(
        go.Scatter(x=df.index, y=df["RSI14"], name="RSI 14", line=dict(color="#ce93d8", width=1.6)),
        row=2,
        col=1,
    )
    fig.add_hline(y=70, line_dash="dot", line_color="#ff5252", row=2, col=1)
    fig.add_hline(y=30, line_dash="dot", line_color="#00c853", row=2, col=1)

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

    auto_refresh = st.toggle("⚡ Automatisk live-uppdatering", value=True)
    refresh_interval = st.selectbox(
        "Uppdateringsintervall",
        options=[60, 120, 300],
        format_func=lambda s: f"Var {s}:e sekund" if s < 120 else f"Var {s // 60}:e minut",
        disabled=not auto_refresh,
    )

    if st.button("🔄 Uppdatera nu", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")
    if st.button("🚪 Logga ut", use_container_width=True):
        st.session_state["authenticated"] = False
        st.rerun()


# ================= LIVE-TERMINAL (AUTO-UPPDATERAD) =================
run_every_setting = refresh_interval if auto_refresh else None


@st.fragment(run_every=run_every_setting)
def render_live_terminal():
    header_col, status_col = st.columns([3, 1.2])
    with header_col:
        st.title("📈 Dividend Terminal — Live")
    with status_col:
        mode_text = f"🟢 LIVE (var {refresh_interval}s)" if auto_refresh else "⏸️ PAUSAD"
        st.markdown(f"**Status:** {mode_text}")
        st.caption(f"Senast uppdaterad: {get_stockholm_time_str()}")

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

    tab_live, tab_port, tab_div, tab_log = st.tabs([
        "📊 Live-grafer & Teknisk Analys",
        "💼 Portfölj & Bevakning",
        "📅 Utdelningskalender",
        "📜 Transaktionslogg",
    ])

    # --- FLIK 1: LIVE-GRAFER ---
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

    # --- FLIK 2: PORTFÖLJ & BEVAKNING ---
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

    # --- FLIK 3: UTDELNINGSKALENDER ---
    with tab_div:
        st.subheader("📅 Utdelningskalender & Direktavkastning")
        include_watchlist = st.checkbox("Inkludera bevakningslistan (Watchlist)", value=False)
        div_tickers = (
            portfolio_tickers + [t for t in watchlist_tickers if t not in portfolio_tickers]
            if include_watchlist
            else portfolio_tickers
        )

        if div_tickers:
            cal_df, payout_df = load_dividend_calendar(div_tickers)

            if not cal_df.empty:
                d1, d2, d3 = st.columns(3)
                avg_yield = cal_df["Direktavkastning (%)"].dropna().mean()
                monthly_cnt = int((cal_df["Frekvens"] == "Månadsvis").sum())
                upcoming = cal_df[cal_df["_sort_days"] < 9999]
                next_ex_str = (
                    f"{upcoming.iloc[0]['Ticker']} ({upcoming.iloc[0]['X-dag']})"
                    if not upcoming.empty
                    else "Ingen kommande registrerad"
                )

                d1.metric("Snitt direktavkastning", f"{avg_yield:.2f}%" if pd.notna(avg_yield) else "—")
                d2.metric("Månadsutdelare", f"{monthly_cnt} st")
                d3.metric("Nästa X-dag", next_ex_str)

                display_cols = [
                    "Ticker",
                    "Bolag",
                    "Frekvens",
                    "Årlig utdelning ($)",
                    "Direktavkastning (%)",
                    "Senaste utbetalning ($)",
                    "X-dag",
                    "Utdelningsdag",
                    "Status X-dag",
                ]
                st.dataframe(cal_df[display_cols], use_container_width=True, hide_index=True)

                chart_col1, chart_col2 = st.columns(2)
                with chart_col1:
                    yield_df = cal_df.dropna(subset=["Direktavkastning (%)"])
                    if not yield_df.empty:
                        fig_yield = px.bar(
                            yield_df.sort_values("Direktavkastning (%)", ascending=False),
                            x="Ticker",
                            y="Direktavkastning (%)",
                            color="Frekvens",
                            template="plotly_dark",
                            title="Direktavkastning per bolag (%)",
                        )
                        st.plotly_chart(fig_yield, use_container_width=True)

                with chart_col2:
                    if not payout_df.empty:
                        month_order = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
                        monthly_agg = (
                            payout_df.groupby(["Månad_nr", "Månad", "Ticker"], as_index=False)["Utdelning ($)"]
                            .sum()
                            .sort_values("Månad_nr")
                        )
                        fig_cal = px.bar(
                            monthly_agg,
                            x="Månad",
                            y="Utdelning ($)",
                            color="Ticker",
                            category_orders={"Månad": month_order},
                            template="plotly_dark",
                            title="Utbetalningsmånader senaste 12 mån ($ per aktie)",
                        )
                        st.plotly_chart(fig_cal, use_container_width=True)
            else:
                st.info("Kunde inte hämta utdelningsdata för valda bolag just nu.")
        else:
            st.info("Inga aktier hittades i `portfolio.json`.")

    # --- FLIK 4: TRANSAKTIONSLOGG ---
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


render_live_terminal()
