"""
Streamlit Task Manager
Features:
- Categories (initial list + add new)
- Tasks with optional deadline
- Automatic color for tasks with deadline (green/yellow/red based on days left)
- For tasks without deadline: initial color set manually, and escalates one level per week since added
- Manual override possible at any time (toggle)
- Ordering: color (red,yellow,green) -> within color tasks with deadline ordered by soonest deadline -> tasks without deadline
- Persistence in SQLite
- Export to Excel

Run:
pip install streamlit pandas openpyxl
streamlit run streamlit_task_manager.py

Note: You can deploy this to Streamlit Community Cloud for mobile access or run locally and use an ngrok tunnel.
"""

# Futuro: conectar con notion.

import streamlit as st
import sqlite3
import pandas as pd
import plotly.express as px
import calendar
from streamlit.components.v1 import html
#import json
from datetime import datetime, date, timedelta
from streamlit_calendar import calendar
#import io
from notion_client import Client
# Mi token de integración de notion
notion = Client(auth=st.secrets["NOTION_TOKEN"])
# IDs de las bases de datos de Notion ocultos de forma segura
dbcharacter_id = st.secrets["DB_CHARACTER_ID"]
dbstats_id = st.secrets["DB_STATS_ID"]
dbboss_id = st.secrets["DB_BOSS_ID"]
dbskills_id = st.secrets["DB_SKILLS_ID"]
dbgym_id = st.secrets["DB_GYM_ID"]
dbquests_id = st.secrets["DB_QUESTS_ID"]
dbexpenses_id = st.secrets["DB_EXPENSES_ID"]
dbbuffs_id = st.secrets["DB_BUFFS_ID"]
dbdebuffs_id = st.secrets["DB_DEBUFFS_ID"]

# Leer elementos de la base de datos

DB_PATH = "tasks.db"

# --- CSS shit ---

 #quita los recuadros de los botones
st.markdown("""
    <style>
    /* Make Streamlit buttons transparent and tight around content */
    div.stButton > button {
        border: none !important;
        background-color: transparent !important;
        padding: 0 !important;
        margin: 0 !important;
        width: auto !important;
        height: auto !important;
        font-size: 24px !important;  /* Emoji size */
        cursor: pointer;
    }
    div.stButton > button:hover {
        background-color: transparent !important;
    }
    </style>
""", unsafe_allow_html=True)


# --- Utility / DB functions ---

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        deadline TEXT, -- ISO date YYYY-MM-DD or NULL
        color TEXT, -- 'green','yellow','red' (user assigned initial or manual)
        manual_override INTEGER DEFAULT 0, -- 0 or 1
        state TEXT DEFAULT 'pendiente', -- 'pendiente' or 'hecho'
        added_date TEXT NOT NULL,
        last_color_set_date TEXT,
        completed_date TEXT
    )
    """)
    
    # 2. MIGRACIÓN: Comprobar si ya existe la columna 'completed_date' en 'tasks'
    # Esto es necesario porque si la tabla ya existía, el CREATE anterior no añade la columna nueva
    cur.execute("PRAGMA table_info(tasks)")
    columns = [info[1] for info in cur.fetchall()]
    if 'completed_date' not in columns:
        try:
            cur.execute("ALTER TABLE tasks ADD COLUMN completed_date TEXT")
            print("Base de datos actualizada: Columna 'completed_date' añadida a 'tasks'.")
        except Exception as e:
            print(f"Nota sobre migración tasks: {e}")
    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tasks_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        original_id INTEGER,
        name TEXT,
        category TEXT,
        deadline TEXT,
        color TEXT,
        state TEXT,
        added_date TEXT,
        completed_date TEXT,
        archived_date TEXT
    )
    """)
    
    # 4. MIGRACIÓN: Comprobar si tasks_history existía (por si acaso) y le falta la columna
    cur.execute("PRAGMA table_info(tasks_history)")
    h_columns = [info[1] for info in cur.fetchall()]
    if 'completed_date' not in h_columns:
        try:
            cur.execute("ALTER TABLE tasks_history ADD COLUMN completed_date TEXT")
            print("Base de datos actualizada: Columna 'completed_date' añadida a 'tasks_history'.")
        except Exception as e:
            print(f"Nota sobre migración history: {e}")

    # 5. Crear tabla categorías
    cur.execute("""
    CREATE TABLE IF NOT EXISTS categories (
        name TEXT PRIMARY KEY
    )
    """)

    # seed categories if empty
    cur.execute("SELECT COUNT(*) FROM categories")
    if cur.fetchone()[0] == 0:
        default_cats = [
            'TECNOLOGÍA','PROFESIÓN','TALLER','ANÁLISIS','GEOMETRÍA','EDPS','CRIPTOGRAFÍA','OTROS'
        ]
        cur.executemany("INSERT INTO categories(name) VALUES(?)", [(c,) for c in default_cats])

    conn.commit()
    conn.close()


init_db()


# color utilities
COLOR_ORDER = ['red','yellow','green']  # we will sort by index in this order (red highest priority)


def days_until(deadline_iso):
    if not deadline_iso:
        return None
    d = datetime.strptime(deadline_iso, "%Y-%m-%d").date()
    return (d - date.today()).days


def compute_display_color(row):
    """Given a DB row (sqlite3.Row or dict-like) compute the color to display and whether auto or manual.
    Rules:
    - If manual_override == 1 -> use stored color (manual)
    - Else if deadline exists -> automatic based on days left: >=14 green, 7-13 yellow, <7 red
    - Else (no deadline) -> escalate from stored color by 1 level per full week since added_date
    """
    stored_color = row['color'] if row['color'] else 'green'
    manual = bool(row['manual_override'])
    dl = row['deadline']
    added = datetime.strptime(row['added_date'], "%Y-%m-%d").date()

    if manual:
        return stored_color, 'manual'

    if dl:
        dleft = days_until(dl)
        if dleft is None:
            return stored_color, 'auto'
        if dleft < 0:
            # past deadline, treat as most urgent
            return 'red', 'auto'
        if dleft < 7:
            return 'red', 'auto'
        elif dleft < 14:
            return 'yellow', 'auto'
        else:
            return 'green', 'auto'
    else:
        # no deadline: escalate weekly from stored color
        weeks = (date.today() - added).days // 7
        base_idx = COLOR_ORDER.index(stored_color) if stored_color in COLOR_ORDER else 2
        # base index in our ordering is 0=red,1=yellow,2=green but we want to escalate toward red (lower index)
        # calculate escalation by reducing index by weeks
        new_idx = max(0, base_idx - weeks)
        return COLOR_ORDER[new_idx], 'auto'


# DB actions

def add_task(name, category, deadline, color, manual_override, state='Pendiente'):
    conn = get_conn()
    cur = conn.cursor()
    added = date.today().isoformat()
    last_set = date.today().isoformat() if manual_override else None
    cur.execute("""
        INSERT INTO tasks (name, category, deadline, color, manual_override, state, added_date, last_color_set_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (name, category, deadline, color, int(manual_override), state, added, last_set))
    conn.commit()
    conn.close()


def update_task_color(task_id, color, manual_override):
    conn = get_conn()
    cur = conn.cursor()
    last_set = date.today().isoformat() if manual_override else None
    cur.execute("UPDATE tasks SET color=?, manual_override=?, last_color_set_date=? WHERE id=?", (color, int(manual_override), last_set, task_id))
    conn.commit()
    conn.close()


def update_task_state(task_id, state):
    conn = get_conn()
    cur = conn.cursor()
    if state == 'hecho':
        # Guardamos la fecha y hora exacta
        now = datetime.now().isoformat()
        cur.execute("UPDATE tasks SET state=?, completed_date=? WHERE id=?", (state, now, task_id))
    else:
        # Si la desmarcas (vuelve a pendiente), borramos la fecha
        cur.execute("UPDATE tasks SET state=?, completed_date=NULL WHERE id=?", (state, task_id))
    conn.commit()
    conn.close()


def delete_task(task_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.commit()
    conn.close()


def edit_task(task_id, name, category, deadline):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE tasks SET name=?, category=?, deadline=? WHERE id=?", (name, category, deadline, task_id))
    conn.commit()
    conn.close()


def get_tasks(include_done=False):
    conn = get_conn()
    cur = conn.cursor()
    if include_done:
        cur.execute("SELECT * FROM tasks")
    else:
        cur.execute("SELECT * FROM tasks WHERE state='pendiente'")
    rows = cur.fetchall()
    conn.close()
    # convert to list of dicts
    return [dict(r) for r in rows]


def get_categories():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM categories")
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows


def add_category(name):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO categories(name) VALUES(?)", (name,))
        conn.commit()
    except Exception:
        pass
    conn.close()

def delete_category(name):
    """
    Borra una categoría solo si no tiene tareas asociadas.
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        # 1. Comprobar si hay tareas usando esta categoría
        cur.execute("SELECT COUNT(*) FROM tasks WHERE category=?", (name,))
        task_count = cur.fetchone()[0]
        
        if task_count > 0:
            conn.close()
            return False, f"Hay {task_count} tarea(s) en la categoría '{name}'"
        
        # 2. Si no hay tareas, borrar la categoría
        cur.execute("DELETE FROM categories WHERE name=?", (name,))
        conn.commit()
        conn.close()
        return True, f"Categoría '{name}' borrada."

    except Exception as e:
        conn.rollback()
        conn.close()
        return False, f"Error al borrar la categoría: {e}"

def archive_and_delete_category(cat_name):
    """
    Mueve todas las tareas de una categoría al historial y borra la categoría.
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        # 1. Copiar tareas a historial
        archived_date = date.today().isoformat()
        cur.execute("""
            INSERT INTO tasks_history (original_id, name, category, deadline, color, state, added_date, completed_date, archived_date)
            SELECT id, name, category, deadline, color, state, added_date, completed_date, ? 
            FROM tasks WHERE category = ?
        """, (archived_date, cat_name))
        
        # 2. Borrar tareas de la tabla principal
        cur.execute("DELETE FROM tasks WHERE category = ?", (cat_name,))
        
        # 3. Borrar la categoría
        cur.execute("DELETE FROM categories WHERE name = ?", (cat_name,))
        
        conn.commit()
        conn.close()
        return True, f"Categoría '{cat_name}' archivada y borrada. Tareas movidas al historial."
    except Exception as e:
        conn.rollback()
        conn.close()
        return False, f"Error al archivar: {e}"
        
def show_stats_page():
    st.header("📊 Estadísticas y Progreso")
    
    conn = get_conn()
    
    # 1. Cargar datos activos y de historial
    df_active = pd.read_sql_query("SELECT * FROM tasks", conn)
    df_history = pd.read_sql_query("SELECT * FROM tasks_history", conn)
    conn.close()
    
    # Unificar columnas para el análisis (ignoramos ID y archived_date por ahora)
    cols = ['name', 'category', 'state', 'color', 'completed_date', 'deadline']
    
    # Asegurarnos de que existen las columnas en ambos (por si la migración falló visualmente)
    # Si acabas de migrar, df_history estará vacío, así que manejamos eso:
    if not df_history.empty:
        df_total = pd.concat([df_active[cols], df_history[cols]], ignore_index=True)
    else:
        df_total = df_active[cols].copy()

    # --- KPIs ---
    total_tasks = len(df_total)
    completed_tasks = df_total[df_total['state'] == 'hecho']
    count_completed = len(completed_tasks)
    
    # Tareas de exámenes/finales completadas
    exams_done = completed_tasks[completed_tasks['color'].isin(['examen', 'final'])]
    count_exams = len(exams_done)
    
    # Columnas de métricas
    kpi1, kpi2, kpi3 = st.columns(3)
    kpi1.metric("Tareas Completadas", count_completed)
    kpi2.metric("Exámenes/Finales Superados", count_exams)
    
    if total_tasks > 0:
        rate = round((count_completed / total_tasks) * 100, 1)
        kpi3.metric("Tasa de Éxito", f"{rate}%")
    else:
        kpi3.metric("Tasa de Éxito", "0%")

    st.markdown("---")

    # --- GRÁFICOS ---
    
    c1, c2 = st.columns(2)
    
    with c1:
        st.subheader("Tareas por Categoría")
        if not df_total.empty:
            # Contamos todas (hechas y pendientes) por categoría
            counts = df_total['category'].value_counts().reset_index()
            counts.columns = ['Categoría', 'Cantidad']
            fig_bar = px.bar(counts, x='Cantidad', y='Categoría', orientation='h', color='Categoría')
            st.plotly_chart(fig_bar, use_container_width=True)
        else:
            st.info("Sin datos.")

    with c2:
        st.subheader("Evolución (Últimos 30 días)")
        # Solo consideramos tareas que tienen fecha de finalización (completed_date no es nulo)
        df_time = completed_tasks.dropna(subset=['completed_date']).copy()
        
        if not df_time.empty:
            # Convertir a datetime
            df_time['date'] = pd.to_datetime(df_time['completed_date']).dt.date
            # Agrupar por día
            daily_counts = df_time['date'].value_counts().reset_index()
            daily_counts.columns = ['Fecha', 'Tareas']
            daily_counts = daily_counts.sort_values('Fecha')
            
            fig_line = px.line(daily_counts, x='Fecha', y='Tareas', markers=True)
            st.plotly_chart(fig_line, use_container_width=True)
        else:
            st.warning("No hay datos históricos con fecha (empieza a completar tareas hoy para ver la línea).")

    # --- TABLA DE EXÁMENES PASADOS ---
    st.subheader("Histórico de Exámenes y Finales")
    if not exams_done.empty:
        st.dataframe(exams_done[['name', 'category', 'deadline', 'completed_date']], use_container_width=True)
    else:
        st.info("Aún no has completado exámenes o finales.")
# --- Streamlit UI ---

st.set_page_config(page_title="Agenda - Ana", layout='wide')
st.title("📚 Agenda")

# Sidebar: controls + add task
with st.sidebar:
    view_mode = st.radio(
        "Ver",
        options=["Por prioridad", "Por categoría","Calendario", "Estadísticas"],
        index=0  # selecciona por defecto la primera opción
    )
    show_done = st.checkbox("Mostrar tareas hechas", value=False)
    
    with st.expander("Añadir tarea", expanded=False):
        name = st.text_input("Nombre de la tarea")
        categories = get_categories()
        cat = st.selectbox("Categoría", options=categories)

        dl = st.date_input("Fecha límite (opcional)", value=None)
        # allow blank: Streamlit date_input always returns a date; we use a checkbox to enable
        #has_dl = st.checkbox("Tiene fecha límite", value=False)
        has_dl = False if dl == None else True
        if not has_dl:
            dl_val = None
        else:
            dl_val = dl.isoformat()

        initial_color = st.selectbox("Color inicial (si sin fecha) / examen / final", options=['examen', 'final', 'green', 'yellow', 'red'], index=2)
        #manual_override = st.checkbox("Color estático", value=False) mejor voy a dejar que todo se vaya moviendo a rojo para no tener algo siempre sin hacer?
        manual_override = True if initial_color in ["examen", "final"] else False
        if st.button("Agregar tarea"):
            if not name.strip():
                st.warning("La tarea necesita un nombre.")
            else:
                chosen_category = cat
                add_category(chosen_category)
                add_task(name.strip(), chosen_category, dl_val, initial_color, manual_override)
                st.success("Tarea añadida.")
                st.session_state["rerun"] = True
    st.markdown("---")
    new_cat = st.text_input("Crear nueva categoría")
    if new_cat and st.button("Añadir categoría"):
        add_category(new_cat.strip())
        st.success(f"Categoría '{new_cat}' añadida. Selecciónala en el desplegable.")
        st.session_state["rerun"] = True

    # --- INICIO: CÓDIGO AÑADIDO PARA BORRAR ---
    with st.expander("Borrar categoría", expanded=False):
        all_categories = get_categories()
        
        # Opcional: no permitir borrar 'OTROS' si la usas como categoría por defecto
        # categories_to_delete = [cat for cat in all_categories if cat != 'OTROS']
        categories_to_delete = all_categories 

        if not categories_to_delete:
            st.info("No hay categorías para borrar.")
        else:
            cat_to_delete = st.selectbox(
                "Selecciona categoría a borrar", 
                options=categories_to_delete, 
                key="cat_delete_select"
            )
            
            if st.button(f"Borrar '{cat_to_delete}'", key="delete_cat_btn"):
                if cat_to_delete:
                    success, message = delete_category(cat_to_delete)
                    if success:
                        st.success(message)
                        st.session_state["rerun"] = True # Forzar refresco
                    else:
                        st.session_state["archive_candidate"] = cat_to_delete
                        st.error(message)
                else:
                    st.warning("Por favor, selecciona una categoría.")
            # Si hay un candidato para archivar (porque falló el borrado simple)
            if "archive_candidate" in st.session_state and st.session_state["archive_candidate"] == cat_to_delete:
                st.warning(f"⚠️ La categoría **{cat_to_delete}** tiene tareas.")
                st.write("¿Quieres mover esas tareas al historial y borrar la categoría de la agenda actual?")
                
                if st.button("Sí, archivar tareas y borrar categoría"):
                    success, msg = archive_and_delete_category(cat_to_delete)
                    if success:
                        st.success(msg)
                        del st.session_state["archive_candidate"] # Limpiar estado
                        st.session_state["rerun"] = True
                    else:
                        st.error(msg)
    # --- FIN: CÓDIGO AÑADIDO PARA BORRAR ---

    st.markdown("---")
    st.markdown("**Exportar**")
    if st.button("Exportar a Excel (descarga)" ):
        df = pd.DataFrame(get_tasks(include_done=True))
        if not df.empty:
            # clean up columns for export
            df['display_color'] = df.apply(lambda r: compute_display_color(r)[0], axis=1)
            towrite = io.BytesIO()
            df.to_excel(towrite, index=False, engine='openpyxl')
            towrite.seek(0)
            st.download_button("Descargar Excel", data=towrite, file_name="tareas.xlsx", mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        else:
            st.info("No hay tareas para exportar")

# Main area: task list

rows = get_tasks(include_done=show_done)
# compute display colors
for r in rows:
    color, mode = compute_display_color(r)
    r['display_color'] = color
    r['color_mode'] = mode
    # compute days left display
    r['days_left'] = days_until(r['deadline']) if r['deadline'] else None

# Ordering
# map color to priority number
priority_map = {'red':0, 'yellow':1, 'green':2}

asignaturas = ['TECNOLOGÍA','PROFESIÓN','TALLER','ANÁLISIS','GEOMETRÍA','EDPS','CRIPTOGRAFÍA']

def sort_key(r):
    cscore = priority_map.get(r['display_color'], 2)
    # prefer tasks with deadline (0) over no-deadline (1) within same color; and earlier deadlines first
    has_deadline = 0 if r['deadline'] else 1
    dl = datetime.max if not r['deadline'] else datetime.strptime(r['deadline'], "%Y-%m-%d")
    return (cscore, has_deadline, dl)
def mostrar_tarea(r,s,e): #r: row, s: shows category, e: its an exam
    with st.container():
        c = (c22 if e else c11) if c11 else st
        c1, c2, c3, c4, c5 = c.columns([15,1,1,1,1])
        # color badge
        badge = r['display_color']
        if badge == 'red':
            bgcolor = '#ff6961'
        elif badge == 'yellow':
            bgcolor = '#fdfd96'
        elif badge == 'green':
            bgcolor = '#77dd77'
        elif badge == 'final': # Nuevo color para 'final'
            bgcolor = '#453a73' # Azul/Morado Oscuro
        else:
            bgcolor = '#cba0dc'
        #para saber si la info esta abta/cerrada
        fecha_str = r.get('deadline')  # puede ser None
        if fecha_str and r['display_color']=="examen":
            # Convertir y formatear si hay fecha
            fecha_cute = datetime.strptime(fecha_str, "%Y-%m-%d").strftime("%d/%m")
        else:
            fecha_cute = ""
        
        # --- Estado del menú de borrado ---
        # Clave única para saber si estamos borrando ESTA tarea específica
        delete_key = f"confirm_delete_{r['id']}"
        if delete_key not in st.session_state:
            st.session_state[delete_key] = False
        
        if f"show_info_{r['id']}" not in st.session_state:
            st.session_state[f"show_info_{r['id']}"] = False
            
        # --- Visualización de la Tarea ---
        # Si la tarea está cancelada, la mostramos un poco más apagada o tachada visualmente (opcional)
        opacity = "0.6" if r['state'] == 'cancelado' else "1.0"
        
        html_content = ""
        if not s or r['category'] == "OTROS":
             html_content = f"""<div style="background-color:{bgcolor}; opacity:{opacity}; padding:10px; border-radius:10px; margin-bottom:5px"><b>{r['name']}  &nbsp;&nbsp; {fecha_cute} </b></div>"""
        else:
             html_content = f"""<div style="background-color:{bgcolor}; opacity:{opacity}; padding:10px; border-radius:10px; margin-bottom:5px"><b>{r['name']}</b>  &nbsp;&nbsp; {r['category']} &nbsp;&nbsp;&nbsp;&nbsp; <b>{fecha_cute}</b></div>"""
        
        c1.markdown(html_content, unsafe_allow_html=True)
            
        # --- BOTONES ---
        
        # buttons
        if c2.button("✅ " , key=f"done_{r['id']}"):
            update_task_state(r['id'], 'hecho')
            st.session_state["rerun"] = True
        # botón de editar
        if c3.button("✏️ ", key=f"edit_{r['id']}"):
            st.session_state[f"show_edit_{r['id']}"] = True

        # formulario de edición
        if st.session_state.get(f"show_edit_{r['id']}", False):
            with st.form(f"form_edit_{r['id']}"):
                new_name = st.text_input("Nombre", value=r['name'])
                new_cat = st.selectbox(
                    "Categoría",
                    options=get_categories(),
                    index=get_categories().index(r['category']) if r['category'] in get_categories() else 0
                )
                has_dl_edit = st.checkbox("Tiene fecha límite", value=bool(r['deadline']))
                if has_dl_edit:
                    new_dl = st.date_input(
                        "Fecha límite",
                        value=datetime.strptime(r['deadline'], "%Y-%m-%d").date() if r['deadline'] else date.today()
                    )
                    new_dl_val = new_dl.isoformat()
                else:
                    new_dl_val = None

                submitted = st.form_submit_button("Guardar cambios")
                if submitted:
                    edit_task(r['id'], new_name.strip(), new_cat, new_dl_val)
                    st.success("Tarea actualizada")
                    st.session_state[f"show_edit_{r['id']}"] = False  # cerrar formulario
                    st.session_state["rerun"] = True
        if c4.button("❌ ", key=f"del_{r['id']}"):
            delete_task(r['id'])
            st.session_state["rerun"] = True
        # activar/desactivar info
        if c5.button("ℹ️ ", key=f"info_{r['id']}"):
            st.session_state[f"show_info_{r['id']}"] = not st.session_state[f"show_info_{r['id']}"]
        #mostrar info si está activada 
        if st.session_state[f"show_info_{r['id']}"]:
            if r['deadline']:
                c1.info(f"Fecha límite: {r['deadline']} ({r['days_left']} días)  \n {r['state']}  \n Añadida: {r['added_date']} ")
            else:
                c1.info(f"({r['state']}) \n Añadida: {r['added_date']} ")
if view_mode == "Por prioridad":
    rows_sorted = sorted(rows, key=sort_key)
    c11, c0, c22 = st.columns([4,1,4])
    c11.subheader("Tareas")
    if not rows_sorted:
        st.info("No hay tareas.")
    else:
        for r in rows_sorted:
            if r['display_color'] not in ['examen', 'final']: mostrar_tarea(r,True,False)  # función que crea el bloque coloreado y botones
    c22.subheader("Exámenes")
    for r in rows_sorted:
        if r['display_color'] == 'examen': mostrar_tarea(r,True,True)  # función que crea el bloque coloreado y botones


if view_mode == "Por categoría":
    categories = get_categories()  # todas las categorías existentes
    c11 = None
    for cat in categories:
        cat_tasks = [r for r in rows if r['category'] == cat]
        if not cat_tasks:
            continue
        else:
            st.subheader(cat)
            cat_tasks_sorted = sorted(cat_tasks, key=sort_key)
            for r in cat_tasks_sorted:
                if r['display_color'] == 'examen': mostrar_tarea(r,False,True)
                else: mostrar_tarea(r,False,False)

if view_mode == "Calendario":
    # Filtramos solo tareas con fecha límite
    tasks_with_deadline = [r for r in rows if r['deadline']]
    # Convertimos cada tarea a evento de calendario
    events = []
    for r in tasks_with_deadline:
        color = "#4CAF50"  # verde por defecto
        if r["display_color"] == "red":
            color = "#e53935"
        elif r["display_color"] == "yellow":
            color = "#f1c40f"
        elif r["display_color"] == "examen":
            color = "#714e8a"
        elif r["display_color"] == "final": # NUEVO: Color para 'final' en calendario
            color = "#1a237e" # Azul Marino Oscuro (distinto a examen)
        cate = r["category"] if r["category"] in asignaturas else ""
        events.append({
            "title": r["name"],
            "start": r["deadline"],   # fecha YYYY-MM-DD
            "color": color,           # color según prioridad
            "extendedProps": {           # propiedades extra
            "category": cate
            }
        })

    # Mostramos el calendario
    state = calendar(
        events=events,
        options={
            "initialView": "dayGridMonth",
            "locale": "es",           # idioma español
            "height": 600,
            "firstDay": 1 
        },
    )

    # Mostrar info si el usuario clica un evento
    if state and "eventClick" in state:
        st.success(f"Tarea seleccionada: {state['eventClick']['event']['title']} ({state['eventClick']['event']['extendedProps']['category']})")
if view_mode == "Estadísticas":
    show_stats_page()
st.markdown("---")
st.write("Hecho con ❤️ — Puedes desplegar esto en Streamlit Cloud para acceder desde el móvil.")
