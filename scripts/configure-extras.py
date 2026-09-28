"""Generate optional-stack secrets locally, without printing them. Never overwrite."""
import argparse,os,secrets,re
from pathlib import Path
p=argparse.ArgumentParser(description='Создать секреты CRM и сервиса предложений.')
p.add_argument('--output',default='.env.extras');p.add_argument('--crm-domain',required=True);p.add_argument('--mcp-domain',required=True)
a=p.parse_args()
for value in [a.crm_domain,a.mcp_domain]:
 if not re.fullmatch(r'[A-Za-z0-9.-]+',value) or '.' not in value:raise SystemExit('Укажите доменное имя без URL.')
values={'CRM_DOMAIN':a.crm_domain,'MAINTENANCE_DOMAIN':a.mcp_domain,'TWENTY_DB_PASSWORD':secrets.token_hex(24),'TWENTY_ENCRYPTION_KEY':secrets.token_hex(32),'TWENTY_APP_SECRET':secrets.token_hex(32),'MAINTENANCE_TOKEN':secrets.token_urlsafe(36),'CRM_GATE_USER':'owner','CRM_GATE_HASH':'REPLACE_WITH_CADDY_BCRYPT_HASH'}
fd=os.open(a.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f:
 for k,v in values.items():f.write(k+"='"+v+"'\n")
print('Настройки созданы. Добавьте хеш пароля для первоначального входа CRM; см. deployment/twenty/README_RU.md.')
