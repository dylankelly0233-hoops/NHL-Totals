import streamlit as st
import pandas as pd
import requests
import numpy as np
import difflib
import io
from datetime import datetime
from bs4 import BeautifulSoup

# --- CONFIGURATION ---
LEAGUE_AVG_TOTAL = 6.2

# --- 1. DATA FETCHING (Cached) ---

@st.cache_data(ttl=3600)
def get_schedule(date_str):
    url = f"https://api-web.nhle.com/v1/schedule/{date_str}"
    try:
        r = requests.get(url).json()
        games = []
        for day in r.get('gameWeek', []):
            if day['date'] == date_str:
                for game in day['games']:
                    games.append({
                        'home_team': game['homeTeam']['abbrev'],
                        'away_team': game['awayTeam']['abbrev'],
                        'home_id': game['homeTeam']['id'],
                        'away_id': game['awayTeam']['id']
                    })
        return games
    except Exception as e:
        st.error(f"Error fetching schedule: {e}")
        return []

@st.cache_data(ttl=3600)
def get_projected_starters():
    url = "https://www.dailyfaceoff.com/starting-goalies"
    headers = {'User-Agent': 'Mozilla/5.0'}
    starters = {}
    try:
        r = requests.get(url, headers=headers)
        soup = BeautifulSoup(r.content, 'html.parser')
        matchups = soup.find_all('div', class_='starting-goalies_matchup')
        for match in matchups:
            teams = match.find_all('span', class_='logo_ticker')
            goalie_cards = match.find_all('h4', class_='name')
            if len(teams) >= 2 and len(goalie_cards) >= 2:
                away_team = teams[0].text.strip()
                home_team = teams[1].text.strip()
                starters[away_team] = goalie_cards[0].text.strip()
                starters[home_team] = goalie_cards[1].text.strip()
        return starters
    except Exception as e:
        return {}

@st.cache_data(ttl=86400)
def get_active_goalies_db():
    url = "https://api-web.nhle.com/v1/goalie-stats-leaders/current?categories=goalsAgainstAverage&limit=250"
    try:
        r = requests.get(url).json()
        goalies = []
        for g in r.get('goalsAgainstAverage', []):
            name = f"{g['firstName']} {g['lastName']}"
            team = g['teamAbbrev']
            # SIMULATED GSAx (Replace with MoneyPuck merge in production)
            gaa = g['value']
            if gaa < 2.5: gsax = round(np.random.uniform(0.3, 0.9), 2)
            elif gaa < 3.1: gsax = round(np.random.uniform(-0.1, 0.25), 2)
            else: gsax = round(np.random.uniform(-0.6, -0.1), 2)
            goalies.append({'Name': name, 'Team': team, 'GSAx': gsax})
        return pd.DataFrame(goalies)
    except Exception:
        return pd.DataFrame(columns=['Name', 'Team', 'GSAx'])

def reconcile_starters(starters_dict, goalie_df):
    if goalie_df.empty: return starters_dict, goalie_df
    
    official_names = goalie_df['Name'].tolist()
    final_starters = {}
    new_rows = []
    
    for team, scraped_name in starters_dict.items():
        if scraped_name in official_names:
            final_starters[team] = scraped_name
        else:
            matches = difflib.get_close_matches(scraped_name, official_names, n=1, cutoff=0.6)
            if matches:
                final_starters[team] = matches[0]
            else:
                new_rows.append({'Name': scraped_name, 'Team': team, 'GSAx': 0.00})
                final_starters[team] = scraped_name
                
    if new_rows:
        goalie_df = pd.concat([goalie_df, pd.DataFrame(new_rows)], ignore_index=True)
    
    # Generic Fallbacks
    goalie_df = pd.concat([goalie_df, pd.DataFrame([
        {'Name': 'Average Goalie', 'Team': 'NHL', 'GSAx': 0.00},
        {'Name': 'Backup/Rookie', 'Team': 'NHL', 'GSAx': -0.40}
    ])], ignore_index=True)
    
    return final_starters, goalie_df.drop_duplicates(subset=['Name']).sort_values('Name')

def get_simulated_ratings(active_teams):
    data = []
    for t in active_teams:
        # SIMULATED TEAM STRENGTH
        off_rating = np.random.uniform(2.9, 3.4) 
        def_rating = np.random.uniform(2.9, 3.4) 
        data.append({'team': t, 'off_rating': off_rating, 'def_rating': def_rating})
    return pd.DataFrame(data).set_index('team')

# --- 2. UI HELPERS ---

def get_gsax(goalie_name, goalie_df):
    try:
        return goalie_df.loc[goalie_df['Name'] == goalie_name, 'GSAx'].values[0]
    except:
        return 0.0

# --- 3. MAIN APP ---

def main():
    st.set_page_config(page_title="NHL Smart Projections", page_icon="🏒", layout="wide")
    
    st.title("🏒 NHL Totals Projection Engine")
    st.markdown("Use the dropdowns below to verify goalie impacts live.")

    # -- Sidebar / Control Panel --
    with st.expander("⚙️ Settings & Date", expanded=True):
        col1, col2 = st.columns([1, 2])
        with col1:
            selected_date = st.date_input("Game Date", datetime.now())
            date_str = selected_date.strftime("%Y-%m-%d")
        with col2:
            st.write(" ") # Spacer
            load_btn = st.button("🔄 Scrape & Load Games", type="primary")

    # -- SESSION STATE MANAGEMENT --
    # We must store data in session_state so it persists when dropdowns are changed
    if load_btn:
        with st.spinner("Fetching Schedule and Starters..."):
            games = get_schedule(date_str)
            if not games:
                st.error("No games found.")
                st.session_state['data_loaded'] = False
            else:
                starters_dict = get_projected_starters()
                raw_goalies = get_active_goalies_db()
                final_starters, final_goalie_db = reconcile_starters(starters_dict, raw_goalies)
                
                active_teams = set([g['home_team'] for g in games] + [g['away_team'] for g in games])
                ratings = get_simulated_ratings(active_teams)
                
                # Store in session state
                st.session_state['games'] = games
                st.session_state['starters'] = final_starters
                st.session_state['goalie_db'] = final_goalie_db
                st.session_state['ratings'] = ratings
                st.session_state['data_loaded'] = True

    # -- DASHBOARD RENDER --
    if st.session_state.get('data_loaded', False):
        games = st.session_state['games']
        goalie_db = st.session_state['goalie_db']
        ratings = st.session_state['ratings']
        starters = st.session_state['starters']
        
        goalie_names = goalie_db['Name'].tolist()

        st.divider()
        st.subheader(f"Games for {date_str}")
        
        for game in games:
            home = game['home_team']
            away = game['away_team']
            gid = game['home_id'] # Use ID for unique keys

            # 1. Calculate Base Total (No Goalies)
            try:
                h_stats = ratings.loc[home]
                a_stats = ratings.loc[away]
                base_total = (h_stats['off_rating'] + a_stats['def_rating'])/2 + \
                             (a_stats['off_rating'] + h_stats['def_rating'])/2
            except:
                base_total = LEAGUE_AVG_TOTAL

            # 2. Setup Container for the "Game Card"
            with st.container(border=True):
                # Columns: Teams | Base Total | Away Goalie | Home Goalie | FINAL PROJ
                c1, c2, c3, c4, c5 = st.columns([1.5, 1, 2, 2, 1.5])
                
                with c1:
                    st.markdown(f"### {away} @ {home}")
                
                with c2:
                    st.metric("Base Total", f"{base_total:.2f}", delta_color="off")
                    st.caption("Before Goalies")

                # 3. Interactive Dropdowns
                # Determine default indices based on scraped starters
                a_start_name = starters.get(away, "Average Goalie")
                h_start_name = starters.get(home, "Average Goalie")
                
                try: a_idx = goalie_names.index(a_start_name)
                except: a_idx = goalie_names.index("Average Goalie")
                
                try: h_idx = goalie_names.index(h_start_name)
                except: h_idx = goalie_names.index("Average Goalie")

                with c3:
                    sel_a_goalie = st.selectbox(f"{away} Goalie", options=goalie_names, index=a_idx, key=f"a_goalie_{gid}")
                    a_gsax = get_gsax(sel_a_goalie, goalie_db)
                    st.caption(f"GSAx: {a_gsax:+.2f}")

                with c4:
                    sel_h_goalie = st.selectbox(f"{home} Goalie", options=goalie_names, index=h_idx, key=f"h_goalie_{gid}")
                    h_gsax = get_gsax(sel_h_goalie, goalie_db)
                    st.caption(f"GSAx: {h_gsax:+.2f}")

                # 4. Final Calculation
                # Logic: Total - HomeGSAx - AwayGSAx
                final_total = base_total - h_gsax - a_gsax
                
                with c5:
                    delta = final_total - base_total
                    st.metric("Proj Total", f"{final_total:.2f}", delta=f"{delta:+.2f}", delta_color="inverse")

if __name__ == "__main__":
    main()
