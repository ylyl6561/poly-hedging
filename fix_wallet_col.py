from smart_money.config import get_settings
import psycopg

cfg = get_settings()
url = cfg.database_url.replace('postgresql+psycopg://', 'postgresql://').replace('postgresql+psycopg2://', 'postgresql://')
conn = psycopg.connect(url, autocommit=True)
cur = conn.cursor()

for tbl in ['smart_money_traders', 'smart_money_leaderboard_entries', 'smart_money_trades',
            'smart_money_current_positions', 'smart_money_position_snapshots', 'smart_money_closed_positions']:
    cur.execute(f'ALTER TABLE {tbl} ALTER COLUMN wallet TYPE VARCHAR(66)')
    print(f'Altered {tbl}.wallet -> VARCHAR(66)')
print('Done')
