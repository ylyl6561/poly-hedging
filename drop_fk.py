from smart_money.config import get_settings
import psycopg

cfg = get_settings()
url = cfg.database_url.replace('postgresql+psycopg://', 'postgresql://').replace('postgresql+psycopg2://', 'postgresql://')
conn = psycopg.connect(url, autocommit=True)
cur = conn.cursor()

# Find all FKs referencing smart_money_markets
cur.execute("""
    SELECT conname, conrelid::regclass
    FROM pg_constraint
    WHERE contype = 'f'
      AND confrelid = 'smart_money_markets'::regclass
""")

constraints = cur.fetchall()
print(f'FK constraints referencing smart_money_markets: {len(constraints)} found')
for row in constraints:
    print(f'  {row[0]} on {row[1]}')

for conname, rel in constraints:
    print(f'Dropping: {conname} on {rel}')
    cur.execute(f'ALTER TABLE {rel} DROP CONSTRAINT {conname}')
print('Done')
