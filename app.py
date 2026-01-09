import streamlit as st
import pandas as pd
import requests
import numpy as np
import difflib
import io
import re  # <--- Added to handle the string cleaning
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
                        'away_id': game['awayTeam']['id'],
                        'home_name': game['homeTeam']['placeName']['default'],
                        'away_name': game['awayTeam']['placeName']['default']
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
                
                # --- HELPER TO CLEAN NAMES ---
                def clean_name(raw_text):
                    # Check if it looks like the messy dict format: "{'default': 'Name'}"
                    if "default" in raw_text and "{" in raw_text:
                        # Extract text inside the quotes for 'default'
                        matches = re.findall(r"'default': '([^']+)'", raw_text)
                        if matches:
                            return " ".join(matches) # Joins "First" and "Last"
                    return raw_text

                starters[away_team] = clean_name(goalie_cards[0].text.strip())
                starters[home_team] = clean_name(goalie_cards[1].text.strip())
                
        return starters
    except Exception:
        return {}

@st.cache_data(ttl=3600)
def get_vegas_odds(api_key, region='us', market='totals'):
    """Fetches odds from The Odds API if a key is provided."""
    if not api_key:
        return {}
    
    url = f"https://api.the-odds-api.com/v4/sports/icehockey_nhl/odds/?apiKey={api_key}&regions={region}&markets={market}&oddsFormat=american"
    try:
        r = requests.get(url).json()
        odds_map = {}
        # Parse the response to find the "Total" (Over/Under)
        for game in r:
            home_team = game.get('home_team')
            # Look for the first bookmaker with a totals market
            for bookmaker in game.get('bookmakers', []):
                for market in bookmaker.get('markets', []):
                    if market['key'] == 'totals':
                        # Usually returns Over and Under. We just need the point (e.g., 6.5)
                        # Take the first outcome's point
                        if len(market['outcomes']) > 0:
                            line = market['outcomes'][0].get('point')
                            odds_map[home_team] = line
                            break
                if home_team in odds_map: break
        return odds_map
    except Exception as e:
        st.warning(f"Could not fetch Vegas odds: {e}")
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
            # SIMULATED GSAx (Replace with real MoneyPuck data in production)
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

def match_vegas_odds(home_team_name, odds_map):
    """Fuzzy matches NHL API team name to Odds API team name."""
    if not odds_map: return 6.5 # Default fallback
    
    # Try exact match first
    if home_team_name in odds_map:
        return odds_map[home_team_name]
    
    # Fuzzy match
    keys = list(odds_map.keys())
    matches = difflib.get_close_matches(home_team_name, keys, n=1, cutoff=0.5)
    if matches:
        return odds_map[matches[0]]
    
    return 6.5 # Default if no match found

# --- 3. UI HELPERS ---

def get_gsax(goalie_name, goalie_df):
    try:
        return goalie_df.loc[goalie_df['Name'] == goalie_name, 'GSAx'].values[0]
    except:
        return 0.0

# --- 4. MAIN APP ---

def main():
    st.set_page_config(page_title="NHL Edge Finder", page_icon="🏒", layout="wide")
    
    st.title("🏒 NHL Edge Finder: Projections vs Vegas")
    st.markdown("Reverse engineer the total, compare it to the live line, and find the edge.")

    # -- Sidebar --
    with st.sidebar:
        st.header("⚙️ Configuration")
        selected_date = st.date_input("Game Date", datetime.now())
        date_str = selected_date.strftime("%Y-%m-%d")
        
        st.divider()
        st.subheader("💰 Vegas Integration")
        odds_api_key = st.text_input("The Odds API Key (Optional)", type="password", help="Get a free key at the-odds-api.com")
        
        st.divider()
        st.subheader("🎯 Betting Strategy")
        edge_threshold = st.slider("Min Edge to Bet", 0.1, 1.5, 0.5, 0.1, help="Difference between Project and Vegas needed to trigger a bet signal.")
        
        load_btn = st.button("🚀 Run Model", type="primary")

    # -- LOGIC --
    if load_btn:
        with st.spinner("Crunching numbers & scraping lines..."):
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
                
                # Fetch Vegas Odds
                vegas_odds = get_vegas_odds(odds_api_key) if odds_api_key else {}
                
                # Store in session state
                st.session_state['games'] = games
                st.session_state['starters'] = final_starters
                st.session_state['goalie_db'] = final_goalie_db
                st.session_state['ratings'] = ratings
                st.session_state['vegas_odds'] = vegas_odds
                st.session_state['data_loaded'] = True

    # -- DASHBOARD --
    if st.session_state.get('data_loaded', False):
        games = st.session_state['games']
        goalie_db = st.session_state['goalie_db']
        ratings = st.session_state['ratings']
        starters = st.session_state['starters']
        vegas_odds = st.session_state['vegas_odds']
        
        goalie_names = goalie_db['Name'].tolist()

        st.subheader(f"📊 Market Analysis for {date_str}")
        
        for game in games:
            home = game['home_team']
            home_full = game['home_name']
            away = game['away_team']
            gid = game['home_id']

            # 1. Base Math
            try:
                h_stats = ratings.loc[home]
                a_stats = ratings.loc[away]
                base_total = (h_stats['off_rating'] + a_stats['def_rating'])/2 + \
                             (a_stats['off_rating'] + h_stats['def_rating'])/2
            except:
                base_total = LEAGUE_AVG_TOTAL

            # 2. Vegas Line Logic (Auto or Manual)
            auto_line = match_vegas_odds(home_full, vegas_odds)
            
            with st.container(border=True):
                # Header
                st.markdown(f"#### {away} @ {home}")
                
                c1, c2, c3, c4 = st.columns([2, 2, 1.5, 1.5])
                
                # Column 1 & 2: Goalies (The Variables)
                a_start = starters.get(away, "Average Goalie")
                h_start = starters.get(home, "Average Goalie")
                
                try: a_idx = goalie_names.index(a_start)
                except: a_idx = goalie_names.index("Average Goalie")
                try: h_idx = goalie_names.index(h_start)
                except: h_idx = goalie_names.index("Average Goalie")

                with c1:
                    sel_a_goalie = st.selectbox(f"{away} Goalie", goalie_names, index=a_idx, key=f"a_{gid}")
                    a_gsax = get_gsax(sel_a_goalie, goalie_db)
                with c2:
                    sel_h_goalie = st.selectbox(f"{home} Goalie", goalie_names, index=h_idx, key=f"h_{gid}")
                    h_gsax = get_gsax(sel_h_goalie, goalie_db)

                # Column 3: The Calculation
                my_proj = base_total - h_gsax - a_gsax
                
                with c3:
                    # Allow user to manually change Vegas line if the API was wrong/missing
                    vegas_line = st.number_input("Vegas Line", value=float(auto_line), step=0.5, key=f"v_{gid}")
                    st.metric("My Projection", f"{my_proj:.2f}")

                # Column 4: The Decision (Edge)
                edge = my_proj - vegas_line
                abs_edge = abs(edge)
                
                with c4:
                    st.write("### Signal")
                    if abs_edge >= edge_threshold:
                        if edge > 0:
                            st.success(f"**BET OVER** (+{abs_edge:.2f})")
                        else:
                            st.error(f"**BET UNDER** ({edge:.2f})")
                    else:
                        st.caption(f"No Value (Edge: {edge:.2f})")

if __name__ == "__main__":
    main()
