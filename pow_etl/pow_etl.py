#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
pow_etl.py
ETL adicional para el tablero PowerBI (FBD/Fratelli): InterBase -> MySQL,
para las piezas que NO pasan por POW_GENERA_SCRIPT / UDMG2Kweb -- MAS la
pieza 'legado', que SI es POW_GENERA_SCRIPT, migrada desde UDMG2Kweb.pas
(7-oct-2026) para no depender de que G2K.exe este abierto con su timer
interno activo (hasta 3 horarios configurables en la solapa Config de
UFMG2Kweb). El SP de InterBase no se toco, se sigue llamando igual.

Piezas soportadas hoy:
    agentes   -> POW_GENERA_AGENTES      -> pow_agentes (DIM_VENDEDOR)
    compras   -> POW_GENERA_COMPRAS      -> pow_compras_cab/det (FACT_COMPRAS / Sell In)
    stock     -> POW_GENERA_STOCK        -> pow_stock (FACT_STOCK, foto a la fecha --hasta)
    articulos -> POW_ENRIQUECE_ARTICULOS -> pow_articulos_ext (enriquecimiento DIM_PRODUCTO)
    clientes  -> POW_ENRIQUECE_CLIENTES  -> pow_clientes_ext (enriquecimiento DIM_CLIENTE)
    legado    -> POW_GENERA_SCRIPT       -> pow_clientes/pow_fac_cab/pow_fac_det/
                 pow_cli_agentes/pow_articulos (flujo viejo de UDMG2Kweb, las 4
                 piezas GENERA_CLI/FAC/AGE/ART activas, ventana rodante de
                 --legado-dias dias hacia atras desde hoy, default 4 -- mismo
                 valor que tenia SYS_CONSTANTES.G2KWEB_EST_MESES en produccion)

Uso:
    py -3.12-32 pow_etl.py agentes [--dry-run] [-c ruta_config.ini]
    py -3.12-32 pow_etl.py compras --desde 2024-01-01 --hasta 2026-09-14
    py -3.12-32 pow_etl.py stock                 (foto de HOY; usar --hasta para otra fecha)
    py -3.12-32 pow_etl.py legado --dry-run
    py -3.12-32 pow_etl.py legado --legado-dias 7
    py -3.12-32 pow_etl.py all

'stock' es una FOTO, no historico: cada corrida guarda el stock actual de
G2K con fecha = --hasta (hoy por defecto). Para tener serie temporal hay
que correrlo periodicamente (no hay tarea programada para esto todavia).

El script es idempotente: usa INSERT .. ON DUPLICATE KEY UPDATE (no
REPLACE INTO), asi que una corrida repetida no duplica filas ni pisa
columnas que se completen a mano en MySQL (ver los ddl_pow_*.sql). 'legado'
usa los mismos verbos (insert ignore / replace into) que ya traia
POW_GENERA_SCRIPT, sin cambios.
"""

import re
import sys
import os
import argparse
import configparser
import logging
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

import fdb
import mysql.connector

DEFAULT_CONFIG = os.path.join(_here, 'pow_etl.ini')
DEFAULT_DESDE = date(2020, 1, 1)  # primer rango de carga historica; ajustar si hace falta mas atras

# ─── Piezas del ETL ─────────────────────────────────────────────────────────
# nombre -> {sp: procedimiento IB a ejecutar, params: orden de parametros}
# params disponibles: empresa, desde, hasta (ver main() para de donde salen)
PIEZAS = {
    'agentes':   {'sp': 'POW_GENERA_AGENTES',      'params': ['empresa']},
    'compras':   {'sp': 'POW_GENERA_COMPRAS',      'params': ['empresa', 'desde', 'hasta']},
    'stock':     {'sp': 'POW_GENERA_STOCK',        'params': ['empresa', 'hasta']},
    'articulos': {'sp': 'POW_ENRIQUECE_ARTICULOS', 'params': ['empresa']},
    'clientes':  {'sp': 'POW_ENRIQUECE_CLIENTES',  'params': ['empresa']},
}

PIEZA_LEGADO = 'legado'
LEGADO_DIAS_DEFAULT = 4

SQL_LEER_SCRIPT = (
    "SELECT script FROM TMP_POW_SCRIPTS_ETL "
    "ORDER BY grupo, renglon"
)
SQL_LEER_GRUPOS = (
    "SELECT grupo, renglon, script FROM TMP_POW_SCRIPTS_ETL "
    "ORDER BY grupo, renglon"
)
SQL_LEER_LEGADO_GRUPOS = (
    "SELECT DISTINCT grupo FROM TMP_POW_SCRIPTS ORDER BY grupo"
)
SQL_LEER_LEGADO_POR_GRUPO = (
    "SELECT script FROM TMP_POW_SCRIPTS WHERE grupo=?"
)
SQL_EJERCICIO_ACTIVO = (
    "SELECT MAX(EJERCICIO) FROM GEN_EJERCICIOS "
    "WHERE EMPRESA=? AND ACTIVO=1"
)
_RE_TABLA_INTO = re.compile(r'\sinto\s+([a-zA-Z_][a-zA-Z0-9_]*)', re.IGNORECASE)
_RE_TABLA_IGNORE = re.compile(r'ignore\s+([a-zA-Z_][a-zA-Z0-9_]*)', re.IGNORECASE)


def _text(v):
    """TMP_POW_SCRIPTS_ETL.script es CHARACTER SET NONE (bytes ANSI/cp1252
    crudos desde Delphi 5). fdb puede devolverlo como bytes; decodificar."""
    if v is None:
        return ''
    if isinstance(v, bytes):
        return v.decode('cp1252', errors='replace')
    return v


def descodifica_clave(clave):
    """Replica DescodificaClave (UEntorno.pas), el mismo esquema que usa
    G2K1.INI para ClaveBD -- NO es cifrado fuerte, es la misma ofuscacion
    reversible ya usada en todo el sistema (consistente a proposito, para
    no tener 2 esquemas distintos). Solo se recuperan los primeros 8
    caracteres significativos de la password (mismo limite de InterBase
    5.6). El valor cifrado de ClaveBD en G2K1.INI sirve tal cual aca --
    es la misma password, mismo algoritmo."""
    posicion = clave.index(',')
    clave1 = int(clave[:posicion])
    clave2 = int(clave[posicion + 1:])
    bytes_out = []
    for _ in range(4):
        bytes_out.append(clave1 % 256)
        clave1 //= 256
    for _ in range(4):
        bytes_out.append(clave2 % 256)
        clave2 //= 256
    return ''.join(chr(b) for b in bytes_out).rstrip('\x00')


def _password_ib(cfg):
    """password_enc (ofuscada, formato 'nro1,nro2') tiene prioridad sobre
    password en texto plano -- asi se puede migrar un .ini sin romper el
    que todavia tenga la version vieja."""
    enc = cfg.get('interbase', 'password_enc', fallback=None)
    if enc:
        return descodifica_clave(enc)
    return cfg.get('interbase', 'password')


def conectar_ib(cfg):
    gds32 = cfg.get('interbase', 'gds32',
                     fallback=r'C:\Desarrollo\Castro\Compilacion\gds32.dll')
    return fdb.connect(
        host            = cfg.get('interbase', 'host'),
        database        = cfg.get('interbase', 'database'),
        user            = cfg.get('interbase', 'user'),
        password        = _password_ib(cfg),
        sql_dialect     = 1,
        fb_library_name = gds32,
    )


def conectar_mysql(cfg):
    return mysql.connector.connect(
        host     = cfg.get('mysql', 'host'),
        user     = cfg.get('mysql', 'user'),
        password = cfg.get('mysql', 'password'),
        database = cfg.get('mysql', 'database'),
        charset  = 'latin1',
    )


def armar_sentencias(cur_ib):
    """Agrupa los fragmentos de TMP_POW_SCRIPTS_ETL por 'grupo' (ya vienen
    ordenados por renglon dentro de cada grupo) y devuelve una sentencia SQL
    completa por grupo."""
    cur_ib.execute(SQL_LEER_GRUPOS)
    sentencias = []
    grupo_actual = None
    partes = []
    for grupo, renglon, script in cur_ib.fetchall():
        if grupo_actual is not None and grupo != grupo_actual:
            sentencias.append(''.join(partes))
            partes = []
        grupo_actual = grupo
        partes.append(_text(script))
    if partes:
        sentencias.append(''.join(partes))
    return sentencias


def armar_sentencias_legado(cur_ib):
    """Reconstruye sentencias SQL completas desde TMP_POW_SCRIPTS (la tabla
    de staging del flujo viejo POW_GENERA_SCRIPT/UDMG2Kweb). A diferencia de
    TMP_POW_SCRIPTS_ETL, esta tabla no tiene columna 'renglon' y mezcla 2
    patrones de fragmento -- este codigo replica exacto
    TDMG2Kweb.BatchInserts (UDMG2Kweb.pas):
      Patron A: una fila que YA es una sentencia completa, terminada en ';'
        ("insert ignore pow_fac_cab set ...;")
      Patron B: una cabecera sin ';' + N filas de valores + una fila ';'
        aislada que cierra el lote
        ("replace into pow_cli_agentes(...) values ", "(fila1)", ",(fila2)", ";")
    Sin columna de orden explicita dentro de cada grupo -- IMPORTANTE: un
    solo `SELECT ... ORDER BY grupo` sobre toda la tabla NO alcanza, porque
    el motor no garantiza orden estable entre filas con el mismo 'grupo'
    (confirmado con un dry-run real: aparecian filas de VALUES sin su
    cabecera adelante). Hay que leer cada grupo por separado, igual que
    UDMG2Kweb (`xGrupos` + `CargarScript(grupo)`: `WHERE grupo=?grupo` sin
    ORDER BY propio) -- ese patron SI preserva el orden de insercion."""
    cur_ib.execute(SQL_LEER_LEGADO_GRUPOS)
    grupos = [r[0] for r in cur_ib.fetchall()]
    sentencias = []
    acum = ''
    for grupo in grupos:
        cur_ib.execute(SQL_LEER_LEGADO_POR_GRUPO, (grupo,))
        for (script,) in cur_ib.fetchall():
            linha = _text(script).strip()
            if not linha:
                continue
            if linha == ';':
                if acum:
                    sentencias.append(acum)
                    acum = ''
            elif linha.endswith(';'):
                if acum:
                    sentencias.append(acum)
                    acum = ''
                sentencias.append(linha[:-1])
            else:
                acum = linha if not acum else acum + ' ' + linha
    if acum:
        sentencias.append(acum)
    return sentencias


def _tabla_de(sql):
    """Replica ExtraerNombreTabla (UDMG2Kweb.pas): nombre de tabla despues
    de ' into ' o, si no hay, despues de 'ignore '. Solo para loguear que
    se esta enviando, no afecta la ejecucion."""
    m = _RE_TABLA_INTO.search(sql)
    if m:
        return m.group(1).lower()
    m = _RE_TABLA_IGNORE.search(sql)
    if m:
        return m.group(1).lower()
    return ''


def dame_ejercicio_activo(con_ib, empresa):
    cur = con_ib.cursor()
    cur.execute(SQL_EJERCICIO_ACTIVO, (empresa,))
    row = cur.fetchone()
    if not row or row[0] is None:
        raise RuntimeError(
            'No se encontro ejercicio ACTIVO en GEN_EJERCICIOS para empresa %s' % empresa)
    return row[0]


def correr_legado(cfg, log, dry_run, dias):
    """Replica RecargarDatos + EjecutarScript de UDMG2Kweb.pas: llama al
    mismo POW_GENERA_SCRIPT (sin tocarlo) con las 4 piezas GENERA_CLI/FAC/
    AGE/ART activas (igual que la configuracion actual de produccion) y una
    ventana rodante de 'dias' dias hacia atras desde hoy (equivalente a
    SYS_CONSTANTES.G2KWEB_EST_MESES). Pensado para reemplazar el timer
    interno de G2K (UFMain.TratarInformeAutomaticoDeG2Kweb), que requiere
    tener G2K.exe abierto -- esta pieza corre sola via tarea programada."""
    empresa = cfg.getint('gestion', 'empresa')
    hasta = date.today()
    desde = hasta - timedelta(days=dias)

    log.info('=== legado (POW_GENERA_SCRIPT) === desde=%s hasta=%s', desde, hasta)
    con_ib = conectar_ib(cfg)
    try:
        ejercicio = dame_ejercicio_activo(con_ib, empresa)
        log.info('Ejercicio activo: %s', ejercicio)

        cur_ib = con_ib.cursor()
        # En dialecto 1 el DATE de IB es timestamp y las facturas llevan hora:
        # el SP filtra "fecha between desde and hasta", asi que hasta debe ser
        # el fin del dia o se pierden las facturas de ese dia.
        hasta_ib = datetime(hasta.year, hasta.month, hasta.day, 23, 59, 59)
        params = (empresa, ejercicio, desde, hasta_ib, 1, 1, 1, 1)
        log.info('Generando script en IB: execute procedure POW_GENERA_SCRIPT%s', params)
        cur_ib.execute(
            'execute procedure POW_GENERA_SCRIPT(?,?,?,?,?,?,?,?)', params)
        con_ib.commit()

        sentencias = armar_sentencias_legado(cur_ib)
        log.info('%d sentencia(s) a aplicar contra MySQL.', len(sentencias))

        if dry_run:
            for i, s in enumerate(sentencias, 1):
                preview = s if len(s) <= 200 else s[:200] + '...'
                log.info('  [DRY] %d (%d chars): %s', i, len(s), preview)
            return True

        for intento in (1, 2):
            try:
                con_my = conectar_mysql(cfg)
                try:
                    cur_my = con_my.cursor()
                    tablas_vistas = set()
                    for s in sentencias:
                        s = s.strip()
                        if not s:
                            continue
                        if s.endswith(';'):
                            s = s[:-1]
                        tabla = _tabla_de(s)
                        if tabla and tabla not in tablas_vistas:
                            tablas_vistas.add(tabla)
                            log.info('  Enviando: %s', tabla)
                        cur_my.execute(s)
                    con_my.commit()
                    log.info('legado: OK (%d sentencias, %d tablas).',
                              len(sentencias), len(tablas_vistas))
                    return True
                except Exception:
                    con_my.rollback()
                    raise
                finally:
                    con_my.close()
            except Exception as e:
                if intento == 1:
                    log.warning('legado: fallo intento 1 (%s), reintentando conexion...', e)
                else:
                    raise
    finally:
        con_ib.close()


def correr_pieza(nombre, cfg, log, dry_run, valores):
    pieza = PIEZAS[nombre]
    sp = pieza['sp']
    params = tuple(valores[p] for p in pieza['params'])
    placeholders = ','.join(['?'] * len(params))

    log.info('=== %s (%s) ===', nombre, sp)
    con_ib = conectar_ib(cfg)
    try:
        cur_ib = con_ib.cursor()
        log.info('Generando script en IB: execute procedure %s(%s)', sp, params)
        cur_ib.execute(f'execute procedure {sp}({placeholders})', params)
        con_ib.commit()

        sentencias = armar_sentencias(cur_ib)
        log.info('%d sentencia(s) a aplicar contra MySQL.', len(sentencias))

        if dry_run:
            for i, s in enumerate(sentencias, 1):
                preview = s if len(s) <= 200 else s[:200] + '...'
                log.info('  [DRY] grupo %d (%d chars): %s', i, len(s), preview)
            return True

        con_my = conectar_mysql(cfg)
        try:
            cur_my = con_my.cursor()
            for i, s in enumerate(sentencias, 1):
                s = s.strip()
                if not s:
                    continue
                if s.endswith(';'):
                    s = s[:-1]  # mysql.connector no acepta ; final
                cur_my.execute(s)
                log.info('  grupo %d/%d aplicado (%d filas afectadas).',
                          i, len(sentencias), cur_my.rowcount)
            con_my.commit()
            log.info('%s: OK.', nombre)
            return True
        except Exception:
            con_my.rollback()
            raise
        finally:
            con_my.close()
    finally:
        con_ib.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('piezas', nargs='+',
                     choices=list(PIEZAS.keys()) + [PIEZA_LEGADO, 'all'],
                     help='que piezas correr (o "all")')
    ap.add_argument('-c', '--config', default=DEFAULT_CONFIG)
    ap.add_argument('--dry-run', action='store_true',
                     help='genera el script y lo muestra, no toca MySQL')
    ap.add_argument('--desde', type=lambda s: datetime.strptime(s, '%Y-%m-%d').date(),
                     default=DEFAULT_DESDE, help='fecha desde (AAAA-MM-DD), piezas con rango de fechas')
    ap.add_argument('--hasta', type=lambda s: datetime.strptime(s, '%Y-%m-%d').date(),
                     default=date.today(), help='fecha hasta (AAAA-MM-DD), piezas con rango de fechas')
    ap.add_argument('--legado-dias', type=int, default=LEGADO_DIAS_DEFAULT,
                     help='solo para la pieza "legado": dias hacia atras desde hoy (default %d)'
                          % LEGADO_DIAS_DEFAULT)
    args = ap.parse_args()

    log = logging.getLogger('pow_etl')
    log.setLevel(logging.INFO)
    log_path = os.path.join(_here, 'pow_etl.log')
    fh = RotatingFileHandler(log_path, maxBytes=2*1024*1024, backupCount=5, encoding='utf-8')
    fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter('%(message)s'))
    log.addHandler(sh)

    if not os.path.exists(args.config):
        log.error('No se encontro %s. Copiar pow_etl.ini.example a pow_etl.ini '
                   'y completar con los datos reales.', args.config)
        sys.exit(1)

    cfg = configparser.ConfigParser()
    cfg.read(args.config, encoding='utf-8')

    piezas = list(PIEZAS.keys()) + [PIEZA_LEGADO] if 'all' in args.piezas else args.piezas
    valores = {
        'empresa': cfg.getint('gestion', 'empresa'),
        'desde': args.desde,
        'hasta': args.hasta,
    }

    ok = True
    for pieza in piezas:
        try:
            if pieza == PIEZA_LEGADO:
                resultado = correr_legado(cfg, log, args.dry_run, args.legado_dias)
            else:
                resultado = correr_pieza(pieza, cfg, log, args.dry_run, valores)
            if not resultado:
                ok = False
        except Exception as e:
            log.error('ERROR en %s: %s', pieza, e, exc_info=True)
            ok = False

    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
