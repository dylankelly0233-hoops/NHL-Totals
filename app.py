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


# --- 1. DATA FETCHING (CACHED) ---

@st.cache_data(ttl=3600)  # Cache data for 1 hour to avoid spamming APIs
def get_schedule(date_str):
    """Fetches schedule from NHL API."""
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
    """
    Scrapes DailyFaceoff for today's projected starters.
    Returns dict: {'NYR': 'Igor Shesterkin', ...}
    """
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
                # DFO is typically Away Team first, Home Team second
                away_team = teams[0].text.strip()
                home_team = teams[1].text.strip()
                away_goalie = goalie_cards[0].text.strip()
                home_goalie = goalie_cards[1].text.strip()

                starters[away_team] = away_goalie
                starters[home_team] = home_goalie

        return starters
    except Exception as e:
        st.warning(f"Could not scrape starters ({e}). Defaulting to averages.")
        return {}


@st.cache_data(ttl=86400)  # Cache for 24 hours (Player list rarely changes)
def get_active_goalies_db():
    """
    Fetches official NHL active goalies to build the master dropdown list.
    """
    url = "https://api-web.nhle.com/v1/goalie-stats-leaders/current?categories=goalsAgainstAverage&limit=250"
    try:
        r = requests.get(url).json()
        goalies = []
        for g in r.get('goalsAgainstAverage', []):
            name = f"{g['firstName']} {g['lastName']}"
            team = g['teamAbbrev']

            # --- SIMULATE GSAx (REPLACE WITH REAL MONEYPUCK DATA IN PRODUCTION) ---
            gaa = g['value']
            if gaa < 2.5:
                gsax = round(np.random.uniform(0.3, 0.9), 2)
            elif gaa < 3.1:
                gsax = round(np.random.uniform(-0.1, 0.25), 2)
            else:
                gsax = round(np.random.uniform(-0.6, -0.1), 2)

            goalies.append({'Name': name, 'Team': team, 'GSAx': gsax})

        return pd.DataFrame(goalies)
    except Exception as e:
        st.error(f"Error fetching goalie DB: {e}")
        return pd.DataFrame(columns=['Name', 'Team', 'GSAx'])


def reconcile_starters(starters_dict, goalie_df):
    """
    Ensures that every projected starter exists in the goalie_df.
    """
    if goalie_df.empty:
        return starters_dict, goalie_df

    official_names = goalie_df['Name'].tolist()
    final_starters = {}
    new_rows = []

    for team, scraped_name in starters_dict.items():
        # 1. Exact Match
        if scraped_name in official_names:
            final_starters[team] = scraped_name
            continue

        # 2. Fuzzy Match
        matches = difflib.get_close_matches(scraped_name, official_names, n=1, cutoff=0.6)
        if matches:
            final_starters[team] = matches[0]
        else:
            # 3. No match (New goalie). Add to DB.
            new_rows.append({'Name': scraped_name, 'Team': team, 'GSAx': 0.00})
            final_starters[team] = scraped_name

    if new_rows:
        goalie_df = pd.concat([goalie_df, pd.DataFrame(new_rows)], ignore_index=True)

    # Always add generic fallbacks
    goalie_df = pd.concat([goalie_df, pd.DataFrame([
        {'Name': 'Average Goalie', 'Team': 'NHL', 'GSAx': 0.00},
        {'Name': 'Backup/Rookie', 'Team': 'NHL', 'GSAx': -0.40}
    ])], ignore_index=True)

    return final_starters, goalie_df.drop_duplicates(subset=['Name']).sort_values('Name')


def get_simulated_ratings(active_teams):
    """Simulates Team Strength (Base Total)"""
    data = []
    for t in active_teams:
        # REPLACE THIS WITH YOUR REAL xG MODEL
        off_rating = np.random.uniform(2.9, 3.4)
        def_rating = np.random.uniform(2.9, 3.4)
        data.append({'team': t, 'off_rating': off_rating, 'def_rating': def_rating})
    return pd.DataFrame(data).set_index('team')


# --- 2. EXCEL GENERATION (IN MEMORY) ---

def create_smart_excel_in_memory(games, goalies_df, ratings, projected_starters, date_str):
    output = io.BytesIO()
    writer = pd.ExcelWriter(output, engine='xlsxwriter')
    workbook = writer.book

    # Formats
    fmt_header = workbook.add_format({'bold': True, 'bg_color': '#D3D3D3', 'border': 1})
    fmt_decimal = workbook.add_format({'num_format': '0.00'})
    fmt_highlight = workbook.add_format({'bg_color': '#FFFFE0', 'border': 1})
    fmt_bold = workbook.add_format({'bold': True, 'border': 1})
    fmt_border = workbook.add_format({'border': 1})

    # --- SHEET 1: PROJECTIONS ---
    ws_main = workbook.add_worksheet('Projections')

    headers = ['Matchup', 'Home Team', 'Away Team', 'Base Total (No Goalies)',
               'Home Goalie', 'H_GSAx', 'Away Goalie', 'A_GSAx', 'FINAL PROJECTION']
    ws_main.write_row('A1', headers, fmt_header)

    # Dropdown Source Range
    goalie_list_formula = '=GoalieDB!$A$2:$A$300'

    for i, game in enumerate(games):
        row = i + 1
        home = game['home_team']
        away = game['away_team']

        # 1. Base Total
        try:
            h_stats = ratings.loc[home]
            a_stats = ratings.loc[away]
            base_total = (h_stats['off_rating'] + a_stats['def_rating']) / 2 + \
                         (a_stats['off_rating'] + h_stats['def_rating']) / 2
        except:
            base_total = LEAGUE_AVG_TOTAL

        ws_main.write(row, 0, f"{away} @ {home}", fmt_border)
        ws_main.write(row, 1, home, fmt_border)
        ws_main.write(row, 2, away, fmt_border)
        ws_main.write(row, 3, base_total, fmt_decimal)

        # 2. Determine Pre-filled Goalie
        h_starter = projected_starters.get(home, "Average Goalie")
        a_starter = projected_starters.get(away, "Average Goalie")

        # 3. Write Dropdowns (With Pre-filled values)
        # Format: (first_row, first_col, last_row, last_col, options)
        ws_main.data_validation(row, 4, row, 4, {'validate': 'list', 'source': goalie_list_formula})
        ws_main.write(row, 4, h_starter, fmt_highlight)

        ws_main.data_validation(row, 6, row, 6, {'validate': 'list', 'source': goalie_list_formula})
        ws_main.write(row, 6, a_starter, fmt_highlight)

        # 4. Lookup Formulas
        ws_main.write_formula(row, 5, f'=VLOOKUP(E{row + 1},GoalieDB!$A:$C,3,FALSE)', fmt_decimal)
        ws_main.write_formula(row, 7, f'=VLOOKUP(G{row + 1},GoalieDB!$A:$C,3,FALSE)', fmt_decimal)

        # 5. Final Calculation
        ws_main.write_formula(row, 8, f'=D{row + 1}-F{row + 1}-H{row + 1}', fmt_bold)

    # Columns
    ws_main.set_column('A:A', 20)
    ws_main.set_column('D:D', 20)
    ws_main.set_column('E:E', 25)
    ws_main.set_column('G:G', 25)
    ws_main.set_column('I:I', 18)

    # --- SHEET 2: DATABASE ---
    ws_db = workbook.add_worksheet('GoalieDB')
    ws_db.write_row('A1', ['Name', 'Team', 'GSAx (Simulated)'], fmt_header)

    for r_idx, r_data in enumerate(goalies_df.itertuples(), start=1):
        ws_db.write(r_idx, 0, r_data.Name)
        ws_db.write(r_idx, 1, r_data.Team)
        ws_db.write(r_idx, 2, r_data.GSAx)

    writer.close()
    output.seek(0)
    return output


# --- MAIN APP UI ---

def main():
    st.set_page_config(page_title="NHL Smart Projections", page_icon="🏒")

    st.title("🏒 NHL Totals Projection Engine")
    st.markdown("""
    This tool reverses engineers Vegas totals by using **Goalie GSAx** and **Team xG metrics**.

    1. Click **Generate Dashboard** to scrape today's schedule and projected starters.
    2. Download the Excel file.
    3. Use the **Yellow Dropdowns** in Excel to swap goalies and see how the total changes instantly.
    """)

    # Date Picker (Defaults to Today)
    selected_date = st.date_input("Select Date", datetime.now())
    date_str = selected_date.strftime("%Y-%m-%d")

    if st.button("Generate Dashboard"):
        with st.spinner(f"Scraping Schedule & Goalies for {date_str}..."):

            # 1. Get Schedule
            games = get_schedule(date_str)

            if not games:
                st.error(f"No games found for {date_str}.")
                return

            # 2. Get Data
            starters_dict = get_projected_starters()
            raw_goalies = get_active_goalies_db()

            # 3. Reconcile
            final_starters, final_goalie_db = reconcile_starters(starters_dict, raw_goalies)

            # 4. Run Model
            active_teams = set([g['home_team'] for g in games] + [g['away_team'] for g in games])
            ratings = get_simulated_ratings(active_teams)

            # 5. Create Excel
            excel_file = create_smart_excel_in_memory(games, final_goalie_db, ratings, final_starters, date_str)

            st.success(f"✅ Found {len(games)} games! Dashboard ready.")

            # 6. Download Button
            st.download_button(
                label="📥 Download Excel Dashboard",
                data=excel_file,
                file_name=f"NHL_Projections_{date_str}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )


if __name__ == "__main__":
    main()