from smart_money.config import get_settings
import psycopg

cfg = get_settings()
url = cfg.database_url.replace('postgresql+psycopg://', 'postgresql://').replace('postgresql+psycopg2://', 'postgresql://')
conn = psycopg.connect(url, autocommit=True)
cur = conn.cursor()

cur.execute("""
    SELECT column_name, data_type, character_maximum_length
    FROM information_schema.columns
    WHERE table_name = 'smart_money_traders'
    ORDER BY ordinal_position
""")
print('=== smart_money_traders columns ===')
for r in cur.fetchall():
    print(r)

print()
cur.execute("""
    SELECT column_name, data_type, character_maximum_length
    FROM information_schema.columns
    WHERE table_name = 'smart_money_leaderboard_entries'
    ORDER BY ordinal_position
""")
print('=== smart_money_leaderboard_entries columns ===')
for r in cur.fetchall():
    print(r)

print()
cur.execute("""
    SELECT conname, contype, pg_get_constraintdef(oid)
    FROM pg_constraint
    WHERE conrelid = 'smart_money_traders'::regclass
""")
print('=== smart_money_traders constraints ===')
for r in cur.fetchall():
    print(r)
