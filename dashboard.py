import streamlit as st
import pandas as pd
import requests
from io import StringIO
import plotly.express as px

st.set_page_config(page_title="Dividend Bot Dashboard", page_icon="📈", layout="wide")

# ================= Inställningar =================
st.sidebar.header("⚙️ Inställningar")
GITHUB_TOKEN = st.sidebar.text_input("GitHub Token (PAT)", type="password")
REPO_OWNER = "cnilsson27"  # ÄNDRA TILL DITT GITHUB-NAMN!
REPO_NAME = "dividend-bot"
FILE_PATH = "trade_log.csv"
# =================================================

@st.cache_data(ttl=300) # Sparar datan i minnet i 5 minuter så vi inte spammar GitHub
def load_trade_log(token):
    if not token:
        return None
        
    url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{FILE_PATH}"
    headers = {"Authorization": f"token {token}"}
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        df = pd.read_csv(StringIO(response.text))
        df['Datum'] = pd.to_datetime(df['Datum']) # Gör om text till datum
        return df
    else:
        st.sidebar.error(f"❌ Kunde inte nå GitHub. Felkod: {response.status_code}")
        return None

st.title("📈 Dividend Bot Dashboard")

if not GITHUB_TOKEN:
    st.info("👈 Klistra in din GitHub Token i sidomenyn för att ladda din portföljdata.")
else:
    df = load_trade_log(GITHUB_TOKEN)
    
    if df is not None and not df.empty:
        # --- TOPPMETRIKER ---
        buy_count = len(df[df['Aktion'] == 'KÖP'])
        sell_count = len(df[df['Aktion'] == 'SÄLJ'])
        
        col1, col2, col3 = st.columns(3)
        col1.metric("🟢 Totalt antal Köp", buy_count)
        col2.metric("🔴 Totalt antal Sälj", sell_count)
        col3.metric("📊 Totala Transaktioner", len(df))
        
        st.markdown("---")
        
        # --- GRAFER ---
        st.subheader("Aktivitet över tid")
        
        # Gruppera datan per dag och aktion
        activity = df.groupby(['Datum', 'Aktion']).size().reset_index(name='Antal')
        
        # Rita upp ett interaktivt stapeldiagram med Plotly
        fig = px.bar(
            activity, 
            x="Datum", 
            y="Antal", 
            color="Aktion",
            color_discrete_map={"KÖP": "#28a745", "SÄLJ": "#dc3545"},
            barmode="group",
            title="Köp och Sälj-händelser per dag"
        )
        st.plotly_chart(fig, use_container_width=True)
        
        st.markdown("---")
        
        # --- RÅDATA ---
        st.subheader("Rådata (trade_log.csv)")
        # Sortera nyast överst
        df_sorted = df.sort_values(by="Datum", ascending=False)
        st.dataframe(df_sorted, use_container_width=True)
        
    elif df is not None and df.empty:
        st.warning("trade_log.csv hittades, men den är tom (inga transaktioner ännu).")
