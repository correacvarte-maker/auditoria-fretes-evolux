"""Run locally. Never commit the generated values."""
import getpass
from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

password = getpass.getpass('Senha do administrador (mínimo 14 caracteres): ')
if len(password) < 14:
    raise SystemExit('Use uma senha de pelo menos 14 caracteres.')
if password != getpass.getpass('Confirme a senha: '):
    raise SystemExit('Senhas diferentes.')
print('ADMIN_PASSWORD_HASH=' + generate_password_hash(password, method='scrypt'))
print('TOKEN_ENCRYPTION_KEY=' + Fernet.generate_key().decode())
print('Guarde esses valores somente no gerenciador de segredos/Render. Não salve no Git.')
