# PostgreSQL 16 con el esquema y los datos de ejemplo incluidos en la imagen
# (evita depender de bind-mounts, que dan problemas en plataformas como Coolify).
FROM postgres:16-alpine
COPY init.sql /docker-entrypoint-initdb.d/01_init.sql
