"""
Parser de archivos planos de informes SIESA (formato ancho fijo).

Convenciones numéricas SIESA:
  - Separador de miles: coma  →  23,673.09  →  23673.09
  - Decimal: punto
  - Negativos con guión al final  →  6.000-  →  -6.0
"""

import re
import chardet
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SiesaReport:
    report_code: str = ""
    report_title: str = ""
    company: str = ""
    report_date: str = ""
    filters: dict = field(default_factory=dict)
    columns: list = field(default_factory=list)
    dataframe: Optional[pd.DataFrame] = None
    raw_header: list = field(default_factory=list)
    debug: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Decodificación
# ---------------------------------------------------------------------------

def _decode(raw: bytes) -> str:
    detected = chardet.detect(raw)
    enc = detected.get("encoding") or "latin-1"
    for attempt in (enc, "utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            return raw.decode(attempt)
        except (UnicodeDecodeError, TypeError):
            continue
    return raw.decode("latin-1", errors="replace")


# ---------------------------------------------------------------------------
# Detección de líneas separadoras
# ---------------------------------------------------------------------------

def _is_separator(line: str) -> bool:
    """Línea de guiones/iguales que separa secciones del informe."""
    s = line.strip()
    if len(s) < 10:
        return False
    if not re.fullmatch(r"[+\-=| ]+", s):
        return False
    return "-" in s or "=" in s


def _is_bordered(line: str) -> bool:
    """Línea de encabezado bordeada con |."""
    return line.startswith("|") or line.startswith("| ")


# ---------------------------------------------------------------------------
# Parseo de posiciones de columnas
# ---------------------------------------------------------------------------

def _despace(text: str) -> str:
    """
    Convierte encabezados espaciados 'I T E M' → 'ITEM'.
    Si los tokens no son todos de 1 carácter, devuelve el texto limpio.
    """
    tokens = text.split(" ")
    if all(len(t) <= 1 for t in tokens if t):
        return "".join(tokens)
    return text.strip()


def _find_col_positions(header_line: str) -> list:
    """
    Extrae [(posicion_char, nombre_columna), ...] de la línea de encabezados.

    Regla: separador entre columnas = 2 o más espacios consecutivos.
    Dentro de un nombre de columna puede haber 1 espacio (ej. 'I T E M').

    Las líneas bordeadas con '| ' tienen 2 chars de borde al inicio.
    Las líneas de datos no tienen ese borde, así que al quitar '| '
    las posiciones resultantes coinciden directamente con las de los datos.
    """
    line = header_line

    # Quitar borde izquierdo; las posiciones en 'line' quedan alineadas con datos
    if line.startswith("| "):
        line = line[2:]
    elif line.startswith("|"):
        line = line[1:]

    # Quitar borde derecho
    if line.endswith(" |"):
        line = line[:-2]
    elif line.endswith("|"):
        line = line[:-1]

    # Las filas de datos tienen 1 espacio de prefijo (" 001476...")
    # mientras que el header tiene "| " (2 chars que quitamos).
    # Al quitar 2 chars del header pero los datos solo tienen 1 prefijo,
    # todas las posiciones detectadas quedan 1 char por debajo.
    # offset = 1 corrige ese desplazamiento.
    offset = 1

    cols = []
    i = 0
    n = len(line)

    while i < n:
        # Saltar espacios
        if line[i] == " ":
            i += 1
            continue

        # Inicio de un token de columna
        col_start = i
        last_nonspace = i

        j = i
        while j < n:
            if line[j] != " ":
                last_nonspace = j
                j += 1
            else:
                # Contar espacios consecutivos
                k = j
                while k < n and line[k] == " ":
                    k += 1
                space_count = k - j

                if space_count >= 2:
                    break  # Siempre es separador de columna

                # Espacio simple: separador solo si AMBOS lados son palabras
                # de más de 1 carácter. Así "I T E M" se mantiene unido
                # pero "DIFERENCIA COSTO_UNIT." se divide en dos columnas.

                # Longitud de la palabra que termina aquí
                pk = j - 1
                while pk > col_start and line[pk] != " ":
                    pk -= 1
                prev_word_start = pk + 1 if line[pk] == " " else col_start
                prev_word_len = j - prev_word_start

                # Longitud de la palabra que empieza después del espacio
                nk = k
                while nk < n and line[nk] != " ":
                    nk += 1
                next_word_len = nk - k

                if prev_word_len == 1 and next_word_len == 1:
                    # Ambos lados son letras únicas → dentro de "I T E M"
                    j = k
                else:
                    # Al menos un lado es palabra larga → separador de columna
                    break

        token = line[col_start: last_nonspace + 1]
        cleaned = _despace(token)
        # Saltar tokens que son solo guiones/iguales (divisores visuales, no columnas)
        if cleaned and not re.fullmatch(r"[-=+]+", cleaned):
            cols.append((col_start + offset, cleaned))

        i = last_nonspace + 1

    return cols


def _find_group_spans(col_lines: list) -> list:
    """
    Detecta rangos de grupos desde líneas que tienen separadores '---'.

    Formato SIESA: '--------- C O S T O ---------  ------- V A L O R -------'
    Cada texto entre dos bloques '---' es un nombre de grupo. Su rango en
    posiciones de datos va desde el inicio del '---' izquierdo hasta el fin
    del '---' derecho (con el offset=1 del parser).

    Devuelve [(data_pos_inicio, data_pos_fin, nombre_grupo), ...].
    """
    for line in col_lines:
        inner = line
        if inner.startswith("| "):
            inner = inner[2:]
        elif inner.startswith("|"):
            inner = inner[1:]
        if inner.endswith(" |"):
            inner = inner[:-2]
        elif inner.endswith("|"):
            inner = inner[:-1]

        dash_spans = [(m.start(), m.end()) for m in re.finditer(r"-{3,}", inner)]
        if len(dash_spans) < 2:
            continue

        offset = 1
        groups = []
        for i in range(len(dash_spans) - 1):
            left_start, left_end = dash_spans[i]
            right_start, right_end = dash_spans[i + 1]
            between = inner[left_end:right_start].strip()
            if not between:
                continue
            parts = between.split()
            label = "".join(parts) if all(len(p) == 1 for p in parts) else between.strip()
            if label and not re.fullmatch(r"[-=+\s]+", label):
                groups.append((left_start + offset, right_end + offset, label))

        if groups:
            return groups

    return []


def _merge_col_lines(col_lines: list) -> list:
    """
    Combina las posiciones de columna de TODAS las líneas de encabezado.

    Maneja dos patrones SIESA:
    A) UCIN2062: línea 1 tiene ITEM/DESCRIPCION, línea 2 tiene sub-etiquetas
       y las columnas reales (U.M, FISICO, …).
    B) UCIN3074: línea 1 tiene '--- COSTO ---' y '--- VALOR ---' como grupos;
       línea 2 tiene las columnas reales con nombres que se repiten (UNITARIO,
       TOTAL). Los grupos se usan como prefijo para desambiguar duplicados.

    Reglas de filtrado:
    1. Tokens '---' ya se saltaron en _find_col_positions.
    2. En líneas con '---' (líneas de grupo), solo se agregan columnas que
       aparecen ANTES del primer '---'; el resto son etiquetas de grupo.
    3. Posición duplicada (±4) → sub-encabezado (ej. UBICACION).
       Si el token siguiente está a ≤15 chars, también se salta (→ LOTE).
    4. Posición dentro del span de columnas ya conocidas → sub-etiqueta.
    5. Nombres duplicados entre líneas → prefijo con el grupo correspondiente.
    """
    group_spans = _find_group_spans(col_lines)
    all_cols: list = []

    for line in col_lines:
        positions = _find_col_positions(line)

        # Determinar el límite izquierdo del primer '---' en esta línea
        inner = line
        if inner.startswith("| "):
            inner = inner[2:]
        elif inner.startswith("|"):
            inner = inner[1:]
        m = re.search(r"-{3,}", inner)
        first_dash_data_pos = (m.start() + 1) if m else None  # +1 = offset

        existing = sorted(pos for pos, _ in all_cols)
        skip_until = -1

        for idx, (pos, name) in enumerate(positions):
            if pos <= skip_until:
                continue

            # Regla 2: en líneas de grupo, saltar tokens que son etiquetas de grupo
            if first_dash_data_pos is not None and pos >= first_dash_data_pos:
                continue

            # Regla 3: duplicado por proximidad → sub-encabezado
            if any(abs(pos - k) <= 4 for k in existing):
                if idx + 1 < len(positions) and positions[idx + 1][0] - pos <= 15:
                    skip_until = positions[idx + 1][0]
                continue

            # Regla 4: posición dentro del span de columnas conocidas
            if any(existing[j] < pos < existing[j + 1]
                   for j in range(len(existing) - 1)):
                continue

            all_cols.append((pos, name))
            existing = sorted(pos for pos, _ in all_cols)

    # Regla 5: prefijo de grupo para nombres duplicados
    sorted_cols = sorted(all_cols, key=lambda x: x[0])

    if group_spans:
        name_count: dict = {}
        for _, n in sorted_cols:
            name_count[n] = name_count.get(n, 0) + 1

        prefixed = []
        for pos, name in sorted_cols:
            if name_count.get(name, 1) > 1:
                prefix = next(
                    (gname for gs, ge, gname in group_spans if gs <= pos <= ge),
                    "",
                )
                final = f"{prefix}_{name}" if prefix else name
            else:
                final = name
            prefixed.append((pos, final))
        sorted_cols = prefixed

    # Siempre deduplicar nombres finales (con o sin grupos)
    seen: dict = {}
    result = []
    for pos, name in sorted_cols:
        if name in seen:
            seen[name] += 1
            result.append((pos, f"{name}_{seen[name]}"))
        else:
            seen[name] = 1
            result.append((pos, name))

    return result


# ---------------------------------------------------------------------------
# Conversión de números
# ---------------------------------------------------------------------------

def _looks_numeric(s: str) -> bool:
    """
    Detecta si un string parece un número SIESA (cantidad o valor monetario).
    Los códigos como '001476' NO son numéricos: no tienen punto decimal,
    no tienen coma de miles, ni guión de negativo al final.
    Sólo convertimos si tiene al menos uno de esos marcadores.
    """
    s = s.strip()
    if not s:
        return False
    has_decimal = "." in s
    has_thousands = "," in s
    has_negative = s.endswith("-")
    return has_decimal or has_thousands or has_negative


def _to_number(s: str):
    """
    Convierte número SIESA a float Python.
    '23,673.09'  →  23673.09
    '6.000-'     →  -6.0
    '6.000'      →  6.0
    """
    s = s.strip()
    if not s:
        return None
    negative = s.endswith("-")
    if negative:
        s = s[:-1].strip()
    s = s.replace(",", "")
    try:
        val = float(s)
        return -val if negative else val
    except ValueError:
        return s


# ---------------------------------------------------------------------------
# Extracción de datos por tokens (alineación derecha variable)
# ---------------------------------------------------------------------------

def _extract_row_tokens(ln: str, starts: list, col_names: list) -> dict:
    """
    Asigna tokens (bloques no-espacio) a columnas según el LÍMITE DERECHO.

    En SIESA los números se alinean a la derecha: el último carácter del valor
    cae justo antes del inicio de la siguiente columna. Cuando un número es
    grande puede comenzar ANTES del límite izquierdo nominal (detectado en el
    encabezado), pero NUNCA supera el límite derecho.

    Algoritmo:
    1. Detectar todos los tokens (bloques sin espacios) con sus posiciones.
    2. Asignar cada token a la primera columna cuyo límite derecho >= fin del token.
       Límite derecho de la columna j = starts[j+1] + 1
       (+1 porque el guión de negativo SIESA puede caer exactamente en la
        posición starts[j+1], que es el inicio nominal de la siguiente columna).
    3. Tokens de texto con múltiples palabras (p.ej. descripción) se concatenan.
    """
    n = len(ln)
    n_cols = len(col_names)
    rights = [starts[j + 1] + 1 if j + 1 < n_cols else n + 1 for j in range(n_cols)]

    tokens = []
    i = 0
    while i < n:
        if ln[i] != " ":
            j = i
            while j < n and ln[j] != " ":
                j += 1
            tokens.append((j, ln[i:j]))  # (tok_end, text)
            i = j
        else:
            i += 1

    col_parts: dict = {name: [] for name in col_names}
    for tok_end, tok_text in tokens:
        for j in range(n_cols):
            if tok_end <= rights[j]:
                col_parts[col_names[j]].append(tok_text)
                break

    return {name: (" ".join(parts) if parts else None) for name, parts in col_parts.items()}


# ---------------------------------------------------------------------------
# Extracción de metadata del encabezado
# ---------------------------------------------------------------------------

def _parse_header(bordered_lines: list) -> dict:
    """Extrae empresa, código, título, fecha y filtros del bloque de encabezado."""
    meta = {
        "company": "",
        "report_code": "",
        "report_title": "",
        "report_date": "",
        "filters": {},
    }

    clean = []
    for ln in bordered_lines:
        # Quitar bordes | y espacios extremos
        inner = ln.strip()
        if inner.startswith("|"):
            inner = inner[1:]
        if inner.endswith("|"):
            inner = inner[:-1]
        clean.append(inner.strip())

    if not clean:
        return meta

    # Línea 1: versión | empresa | fecha
    first = clean[0]
    date_m = re.search(r"FECHA\s*:\s*(\S+)", first, re.IGNORECASE)
    if date_m:
        meta["report_date"] = date_m.group(1)

    # Empresa: texto central grande (entre versión y FECHA)
    company_m = re.search(
        r"(?:VER\s+\S+\.?\s+)(.+?)(?:\s{3,}FECHA)", first, re.IGNORECASE
    )
    if company_m:
        meta["company"] = company_m.group(1).strip()

    # Línea 2: código de informe y título
    if len(clean) > 1:
        second = clean[1]
        code_m = re.match(r"([A-Z]{2,}\d+\.\w+)", second.strip())
        if code_m:
            meta["report_code"] = code_m.group(1)
        title_part = re.sub(r"HORA\s*:.*", "", second).strip()
        title_part = re.sub(r"^[A-Z]{2,}\d+\.\w+\s*", "", title_part).strip()
        if title_part:
            meta["report_title"] = title_part

    # Filtros: líneas con patrón "Clave : Valor"
    for ln in clean[2:]:
        filter_m = re.match(r"([^:]{3,30}?)\s*:\s+(.+)", ln)
        if filter_m:
            key = filter_m.group(1).strip()
            val = filter_m.group(2).strip()
            if key and val and len(key) < 30:
                meta["filters"][key] = val

    return meta


# ---------------------------------------------------------------------------
# Parser principal
# ---------------------------------------------------------------------------

def parse(raw: bytes) -> SiesaReport:
    """
    Parsea un archivo plano de SIESA y devuelve un SiesaReport.
    El DataFrame dentro del reporte tiene los datos limpios y los números
    ya convertidos a float.
    """
    content = _decode(raw)
    lines = content.splitlines()
    report = SiesaReport()

    # Índices de líneas separadoras
    sep_idx = [i for i, ln in enumerate(lines) if _is_separator(ln)]

    if len(sep_idx) < 2:
        # Sin separadores: intentar parseo simple por espacios
        return _fallback_parse(lines, report)

    # ---- Localizar la sección de encabezados de columna desde el INICIO ----
    # Estructura típica SIESA:
    #   sep[0] → info empresa/filtros → sep[1] → nombres columnas → sep[2] → datos
    # El "FIN LISTADO" y totales al final también tienen separadores, por eso
    # NO se puede usar sep[-1] y sep[-2] — hay que buscar desde arriba.

    col_h_sep_before = None  # índice dentro de sep_idx que precede la fila de columnas
    col_h_sep_after  = None  # índice dentro de sep_idx que sigue a la fila de columnas

    for k in range(len(sep_idx) - 1):
        s1 = sep_idx[k]
        s2 = sep_idx[k + 1]
        between = [ln for ln in lines[s1 + 1: s2] if ln.strip()]

        if not between:
            continue

        # La sección de nombres de columna:
        # - Todas las líneas empiezan con |
        # - NO contiene ":" (los filtros sí lo tienen)
        # - Tiene palabras en MAYÚSCULAS (nombres de columna)
        all_bordered = all(_is_bordered(ln) for ln in between)
        has_colon = any(":" in ln for ln in between)

        if all_bordered and not has_colon:
            # Verificar que haya al menos 3 palabras mayúsculas (nombres de col)
            for ln in between:
                inner = ln.strip().strip("|").strip()
                tokens = [t for t in inner.split()
                          if t.replace(".", "").replace("_", "").replace("-", "").isalpha()
                          and (t.isupper() or len(t) == 1)]
                if len(tokens) >= 3:
                    col_h_sep_before = k
                    col_h_sep_after  = k + 1
                    break
        if col_h_sep_before is not None:
            break

    # Si no encontramos la sección de columnas, intentar con los dos primeros sep
    if col_h_sep_before is None:
        if len(sep_idx) >= 3:
            col_h_sep_before, col_h_sep_after = 1, 2
        else:
            col_h_sep_before, col_h_sep_after = 0, 1

    # ---- Bloque de metadata (todo antes del separador que precede columnas) ----
    header_bordered = [
        ln for ln in lines[: sep_idx[col_h_sep_before]]
        if _is_bordered(ln) and not _is_separator(ln)
    ]
    report.raw_header = header_bordered

    meta = _parse_header(header_bordered)
    report.company     = meta["company"]
    report.report_code = meta["report_code"]
    report.report_title = meta["report_title"]
    report.report_date = meta["report_date"]
    report.filters     = meta["filters"]

    # ---- Encabezados de columnas ----
    col_header_lines = [
        ln for ln in lines[sep_idx[col_h_sep_before] + 1: sep_idx[col_h_sep_after]]
        if ln.strip() and not _is_separator(ln)
    ]

    if not col_header_lines:
        return report

    # Combinar posiciones de TODAS las líneas de encabezado de columna.
    # SIESA a veces pone ITEM/DESCRIPCION en la línea 1 y U.M/FISICO/etc. en la 2.
    col_positions = _merge_col_lines(col_header_lines)

    if not col_positions:
        return report

    report.columns = [name for _, name in col_positions]
    starts = [pos for pos, _ in col_positions]

    # Guardar diagnósticos para depuración en la UI
    report.debug = {
        "sep_indices": sep_idx[:10],  # primeros 10 separadores
        "col_h_sep_before": col_h_sep_before,
        "col_h_sep_after": col_h_sep_after,
        "col_header_lines_raw": col_header_lines,
        "col_positions": col_positions,
        "data_start_line": sep_idx[col_h_sep_after] + 1,
    }

    # ---- Filas de datos (desde el separador que sigue a los encabezados) ----
    data_lines = lines[sep_idx[col_h_sep_after] + 1:]
    rows = []

    for ln in data_lines:
        # Saltar líneas vacías, separadores o encabezados repetidos (paginación)
        if not ln.strip():
            continue
        if _is_separator(ln):
            continue
        if _is_bordered(ln):
            continue

        raw_cells = _extract_row_tokens(ln, starts, report.columns)
        row = {}
        for col_name in report.columns:
            cell = (raw_cells.get(col_name) or "").strip()
            if _looks_numeric(cell):
                row[col_name] = _to_number(cell)
            else:
                row[col_name] = cell if cell else None

        # Ignorar filas completamente vacías
        if any(v is not None and v != "" for v in row.values()):
            rows.append(row)

    if rows:
        report.dataframe = pd.DataFrame(rows, columns=report.columns)

    return report


def _fallback_parse(lines: list, report: SiesaReport) -> SiesaReport:
    """Parseo de último recurso para archivos sin separadores claros."""
    data_lines = [ln for ln in lines if ln.strip() and not _is_bordered(ln)]
    if not data_lines:
        return report

    # Intentar dividir por múltiples espacios
    rows = []
    for ln in data_lines:
        parts = re.split(r"\s{2,}", ln.strip())
        if parts:
            rows.append(parts)

    if not rows:
        return report

    max_cols = max(len(r) for r in rows)
    columns = [f"COL_{i+1}" for i in range(max_cols)]
    report.columns = columns

    normalized = [r + [None] * (max_cols - len(r)) for r in rows]
    report.dataframe = pd.DataFrame(normalized, columns=columns)
    return report
