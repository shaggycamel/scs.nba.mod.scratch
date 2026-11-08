import scs_hub

hub = scs_hub.SportsHub(db_con="postgres")
df_tables = hub.db.read("""
    select *
    from information_schema.tables
    where table_schema in ('nba', 'statyx', 'util')
        and ((table_schema != 'util' and table_type = 'BASE TABLE')
            or (table_schema = 'util' and table_type = 'VIEW'))
            and table_name not ilike '%retired' 
""")

for row in df_tables.iter_rows(named=True):
    qry = f"select * from {row['table_schema']}.{row['table_name']}"
    df = hub.db.read(qry)
    df.write_parquet(f"data/{row['table_schema']}/{row['table_name']}.parquet")
print(qry)
