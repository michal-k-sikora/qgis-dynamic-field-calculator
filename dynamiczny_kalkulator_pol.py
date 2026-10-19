# -*- coding: utf-8 -*-
from typing import Optional, Dict, Tuple
import json
import os
import unicodedata
from contextlib import contextmanager

from qgis.PyQt.QtWidgets import (
    QDockWidget, QVBoxLayout, QHBoxLayout, QWidget, QComboBox, QPushButton,
    QLabel, QLineEdit, QListWidget, QCheckBox, QMessageBox, QAction, QScrollArea,
    QListWidgetItem, QSizePolicy, QSpacerItem, QToolButton, QToolBar, QGroupBox, QInputDialog,
    QSplitter
)
from qgis.PyQt.QtCore import (
    Qt, QObject, QTimer, pyqtSlot, QSize, QEvent,
    QCoreApplication, QTranslator, QSettings, QVariant, QMetaType, QByteArray,
    QDate, QTime, QDateTime
)
from qgis.PyQt.QtGui import QIcon, QPixmap, QPainter, QFont, QColor
from qgis.PyQt import sip

from qgis.core import (
    QgsProject, QgsVectorLayer, QgsExpression, QgsExpressionContext,
    QgsExpressionContextUtils, QgsSettings, QgsFields,
    QgsApplication, QgsMessageLog, Qgis, QgsFeatureRequest, QgsField,
    QgsVectorDataProvider
)
from qgis.gui import QgsExpressionBuilderDialog, QgsFilterLineEdit

# ──────────────────────────────────────────────────────────────────────────────
# ICONS – loaded from SVG files (no compiled QRC: pyrcc5 output imports PyQt5)
ICONS_DIR = os.path.join(os.path.dirname(__file__), "icons")
PLUGIN_ICON_PATH = os.path.join(ICONS_DIR, "icon.svg")
CALC_BUTTON_ICON_PATH = os.path.join(ICONS_DIR, "calc_button.svg")

# ──────────────────────────────────────────────────────────────────────────────
# i18n (Qt) settings
PLUGIN_I18N_BASENAME = "dynamiczny_kalkulator_pol"  # file base for i18n/*.qm

# GUI names (source language: EN)
TOOLBAR_OBJECT_NAME = "DynamicFieldCalculatorToolbar"
LEGACY_TOOLBAR_OBJECT_NAMES = ("OnGeoToolbar", "NarzędziaOnGeoToolbar")
# QSettings (internal keys - do not translate)
ORG = "DynamicznyKalkulatorPol"
KEY_LAST = "last_settings_json"
KEY_SPLITTER_STATE = "dock_columns_splitter_state"

# QgsSettings namespace for persistent named configurations (Field Sets)
KEY_FIELDSETS_ROOT = "DynamicFieldCalculator/field_sets"

# QGIS 3.38 moved field origins to Qgis; retain support for QGIS 3.34.
_EXPRESSION_FIELD_ORIGIN = (
    Qgis.FieldOrigin.Expression
    if hasattr(Qgis, "FieldOrigin")
    else QgsFields.FieldOrigin.OriginExpression
)

# ----------------- helpers -----------------
def _is_deleted(obj) -> bool:
    try:
        if obj is None:
            return True
        return sip.isdeleted(obj)
    except Exception:
        return True

def _log(msg: str):
    """Internal log helper (do not translate log prefix)."""
    try:
        QgsMessageLog.logMessage(f"DKP: {msg}", "Plugins", Qgis.MessageLevel.Info)
    except Exception:
        pass

def _fold_text(text: str) -> str:
    """Case- and diacritics-insensitive form used by the layer filter ("Źródła" -> "zrodla")."""
    text = unicodedata.normalize("NFKD", text.casefold().replace("ł", "l"))
    return "".join(c for c in text if not unicodedata.combining(c))

def _draw_text_icon(txt: str) -> QIcon:
    pm = QPixmap(24, 24); pm.fill(QColor(245,245,247))
    p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(QColor(60,60,70)); p.setFont(QFont("Segoe UI", 10, QFont.Weight.Medium))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, txt); p.end()
    return QIcon(pm)

def _plugin_icon() -> QIcon:
    # Plugin icon from the bundled SVG file
    forced = QIcon(PLUGIN_ICON_PATH)
    if not forced.isNull():
        return forced
    return _draw_text_icon("ε")

def _theme_icon_first(names) -> QIcon:
    for n in names:
        ico = QgsApplication.getThemeIcon(n)
        if not ico.isNull():
            return ico
    return QIcon()

def _get_or_create_toolbar(iface, title: str, object_name: str) -> QToolBar:
    """Find this plugin's toolbar by its stable object name or create it."""
    mw = iface.mainWindow()
    for tb in mw.findChildren(QToolBar):
        if tb.objectName() == object_name:
            # During a ZIP upgrade, unload() removes the old toolbar before
            # deleteLater() runs. Do not reuse that detached toolbar.
            if mw.toolBarArea(tb) == Qt.ToolBarArea.NoToolBarArea:
                continue
            tb.setWindowTitle(title)
            tb.setToolTip(title)
            if tb.toggleViewAction():
                tb.toggleViewAction().setText(title)
            return tb

    tb = iface.addToolBar(title)
    tb.setObjectName(object_name)
    tb.setWindowTitle(title)
    tb.setToolTip(title)
    tb.setMovable(True)
    if tb.toggleViewAction():
        tb.toggleViewAction().setText(title)
    return tb


def _remove_empty_legacy_toolbars(iface):
    """Remove empty toolbars left by older versions without touching shared actions."""
    mw = iface.mainWindow()
    for tb in mw.findChildren(QToolBar):
        if tb.objectName() in LEGACY_TOOLBAR_OBJECT_NAMES and not tb.actions():
            mw.removeToolBar(tb)
            tb.deleteLater()

_COMPLEX_FIELD_TYPES = (QMetaType.Type.QVariantMap, QMetaType.Type.QVariantList, QMetaType.Type.QStringList)

def _is_complex_field(f: QgsField) -> bool:
    """JSON / map / array fields (e.g. GPKG JSON, PostgreSQL arrays, hstore)."""
    return f.type() in _COMPLEX_FIELD_TYPES

def _is_json_field(f: QgsField) -> bool:
    """JSON/JSONB column. Depending on GDAL/provider QGIS reports it as Map or StringList
    (e.g. GDAL 3.12 in QGIS 4 reports a GPKG JSON column created from a list as StringList),
    so the column semantics are taken from the type name, not from f.type()."""
    return (f.typeName() or "").lower() in ("json", "jsonb")

def _to_complex_value(val, qgs_field: QgsField) -> Tuple[bool, object]:
    """Prepare an expression result for a complex field without turning it into text.

    Lists/maps are passed as-is (the provider serializes them), JSON text is parsed.
    Returns (False, None) when the value cannot be stored without corrupting the field.
    """
    if val is None or (isinstance(val, QVariant) and val.isNull()):
        return True, None
    if isinstance(val, str):
        if not val.strip():
            return True, None
        try:
            val = json.loads(val)
        except ValueError:
            return False, None
    is_json = _is_json_field(qgs_field)
    is_map = qgs_field.type() == QMetaType.Type.QVariantMap
    # JSON/JSONB: list or dict; map/hstore: dict only; list/stringlist: list only
    if isinstance(val, list) and (is_json or not is_map):
        return True, val
    if isinstance(val, dict) and (is_json or is_map):
        return True, val
    return False, None

def _field_type_info(f: QgsField) -> Tuple[str, QIcon, str]:
    if _is_complex_field(f):
        is_map = f.type() == QMetaType.Type.QVariantMap or _is_json_field(f)
        badge = "{}" if is_map else "[]"
        icon = QIcon()
        if hasattr(QgsFields, "iconForFieldType"):  # not available in older QGIS 3.x
            # Map icon for JSON, in the enum this QGIS reports (QVariant.Type < 3.38, QMetaType.Type later)
            icon_type = type(f.type())(QMetaType.Type.QVariantMap) if is_map else f.type()
            icon = QgsFields.iconForFieldType(icon_type, f.subType(), f.typeName())
        return (badge, _draw_text_icon(badge) if icon.isNull() else icon, f.typeName() or "Complex")

    tn = (f.typeName() or "").lower()
    ico_text = _theme_icon_first(["mIconFieldText.svg"]) or _draw_text_icon("abc")
    ico_int  = _theme_icon_first(["mIconFieldInteger.svg"]) or _draw_text_icon("123")
    ico_real = _theme_icon_first([
        "mIconFieldDecimal.svg", "mIconFieldDouble.svg",
        "mIconFieldNumeric.svg", "mIconFieldFloat.svg", "mIconFieldNumber.svg"
    ]) or _draw_text_icon("1.23")
    ico_dt   = _theme_icon_first(["mIconFieldDateTime.svg"]) or _draw_text_icon("dt")
    ico_d    = _theme_icon_first(["mIconFieldDate.svg"]) or _draw_text_icon("date")
    ico_t    = _theme_icon_first(["mIconFieldTime.svg"]) or _draw_text_icon("time")
    ico_b    = _theme_icon_first(["mIconFieldBoolean.svg"]) or _draw_text_icon("bool")

    # NOTE: these labels are currently not shown directly in the UI, but we keep them in EN.
    if "char" in tn or "text" in tn or "string" in tn:  return ("abc", ico_text, "Text")
    if "int" in tn:                                     return ("123", ico_int, "Integer")
    if any(x in tn for x in ["real","double","float","numeric","decimal"]):
                                                        return ("1.23", ico_real, "Real")
    if "datetime" in tn or ("date" in tn and "time" in tn):
                                                        return ("dt",  ico_dt,  "DateTime")
    if "date" in tn:                                    return ("date",ico_d,   "Date")
    if "time" in tn:                                    return ("time",ico_t,   "Time")
    if "bool" in tn or "bit" in tn:                     return ("bool",ico_b,   "Boolean")
    try:
        if f.isNumeric():                               return ("1.23", ico_real, "Numeric")
    except Exception:
        pass
    return ("?", _draw_text_icon("?"), f.typeName() or "Unknown")

def _coerce_to_field_type(val, qgs_field: QgsField):
    """Gently cast the expression result to the field data type."""
    try:
        tname = (qgs_field.typeName() or "").lower()
        if qgs_field.isNumeric():
            if "int" in tname:
                if val in ("", None): return None
                return int(float(val))
            else:
                if val in ("", None): return None
                return float(val)
        if "bool" in tname:
            if isinstance(val, str):
                return val.strip().lower() in ("1","true","t","yes","y")
            return bool(val)
        if val is None:
            return None
        if isinstance(val, (QDate, QTime, QDateTime)):
            # str(QDateTime) is a Python/PyQt object representation, not a
            # date value. Let QGIS convert temporal results for this field.
            return qgs_field.convertCompatible(val)
        return str(val) if not isinstance(val, (str, bytes)) else val
    except Exception:
        return val

@contextmanager
def block_layer_signals(layer: QgsVectorLayer):
    """Context manager: safely blocks layer signals during modifications."""
    eb = layer.editBuffer()
    try:
        if eb: eb.blockSignals(True)
        layer.blockSignals(True)
        yield
    finally:
        if eb: eb.blockSignals(False)
        layer.blockSignals(False)


def _expression_dependencies(expressions):
    """Return attribute and geometry requirements for prepared expressions.

    ``QgsFeatureRequest.ALL_ATTRIBUTES`` is a sentinel, not a field name.  If
    any expression returns it, the provider request must not limit attributes.
    """
    needed_attrs = set()
    requires_all_attributes = False
    needs_geometry = False

    for exp in expressions:
        referenced = exp.referencedColumns()
        if QgsFeatureRequest.ALL_ATTRIBUTES in referenced:
            requires_all_attributes = True
        else:
            needed_attrs.update(referenced)
        needs_geometry = needs_geometry or exp.needsGeometry()

    return needed_attrs, requires_all_attributes, needs_geometry

# --------------- Plugin main ---------------
class FieldCalcDockPlugin(QObject):
    def __init__(self, iface):
        super().__init__()
        self.iface = iface

        # Keep translator alive (Qt translator must be stored to avoid GC)
        self.translator: Optional[QTranslator] = None
        self._install_translator()

        self.dock: Optional[FieldCalcDock] = None
        self.toolbar_action: Optional[QAction] = None
        self.toolbar = None
        self.menu_root = None
        self._project = QgsProject.instance()
        self._connected = False
        self._lt_name_conn_root = None
        self._lt_name_conn_slot = None

    # ---------- i18n loader ----------
    def _install_translator(self):
        """Load and install the .qm translation matching current QGIS UI locale."""
        try:
            locale_full = QSettings().value('locale/userLocale', 'en')
            locale = str(locale_full)[0:2] if locale_full else "en"
        except Exception:
            locale = "en"

        locale_path = os.path.join(
            os.path.dirname(__file__),
            'i18n',
            f'{PLUGIN_I18N_BASENAME}_{locale}.qm'
        )

        if os.path.exists(locale_path):
            tr = QTranslator()
            if tr.load(locale_path):
                QCoreApplication.installTranslator(tr)
                self.translator = tr
                _log(f"Loaded translation: {locale_path}")
            else:
                _log(f"Failed to load translation file: {locale_path}")
        else:
            # No translation file for this locale -> fall back to source strings (EN)
            self.translator = None

    def initGui(self):
        title = self.tr("Dynamic Field Calculator")
        self.menu_root = title
        self.toolbar = _get_or_create_toolbar(self.iface, title, TOOLBAR_OBJECT_NAME)

        # Plugin action
        self.toolbar_action = QAction(
            QIcon(PLUGIN_ICON_PATH),
            self.tr("Dynamic Field Calculator"),
            self.iface.mainWindow()
        )
        self.toolbar_action.setObjectName("dynamiczny_kalkulator_pol_action")
        self.toolbar_action.setCheckable(True)
        self.toolbar_action.toggled.connect(self._on_action_toggled)

        self.toolbar.addAction(self.toolbar_action)
        self.iface.addPluginToMenu(self.menu_root, self.toolbar_action)
        _remove_empty_legacy_toolbars(self.iface)

        self._connect_project_signals()

        if self.toolbar_action.icon().isNull():
            self.toolbar_action.setIcon(_plugin_icon())

    def unload(self):
        try:
            if self._lt_name_conn_root and self._lt_name_conn_slot:
                self._lt_name_conn_root.nameChanged.disconnect(self._lt_name_conn_slot)
        except Exception:
            pass
        self._lt_name_conn_root = None
        self._lt_name_conn_slot = None

        if self.dock:
            try: self.dock.visibilityChanged.disconnect(self._sync_action_with_dock)
            except Exception: pass
            try: self.dock.destroyed.disconnect(self._on_dock_destroyed)
            except Exception: pass
            # Remove the panel, otherwise it stays in the main window after disable/reload
            if not _is_deleted(self.dock):
                self.iface.removeDockWidget(self.dock)
                self.dock.deleteLater()
            self.dock = None

        if self.toolbar_action:
            try:
                if self.toolbar:
                    self.toolbar.removeAction(self.toolbar_action)

                # Remove from the current menu root
                self.iface.removePluginMenu(self.menu_root, self.toolbar_action)

                # Backward-compat cleanup: remove possible old menu roots from older versions
                try:
                    self.iface.removePluginMenu("OnGeo Tools", self.toolbar_action)
                except Exception:
                    pass
                try:
                    self.iface.removePluginMenu("Narzędzia OnGeo", self.toolbar_action)
                except Exception:
                    pass
                try:
                    self.iface.removePluginMenu("Dynamiczny Kalkulator Pól", self.toolbar_action)
                except Exception:
                    pass
            except Exception:
                pass
            self.toolbar_action.deleteLater()
            self.toolbar_action = None

        if self.toolbar and not _is_deleted(self.toolbar) and not self.toolbar.actions():
            self.iface.mainWindow().removeToolBar(self.toolbar)
            self.toolbar.deleteLater()

        self.toolbar = None
        self.menu_root = None
        self._disconnect_project_signals()

        if self.translator:
            QCoreApplication.removeTranslator(self.translator)
            self.translator = None

    def _connect_project_signals(self):
        if self._connected:
            return
        p = self._project
        p.layersAdded.connect(self._on_layers_changed)
        p.layerWasAdded.connect(self._on_layers_changed)
        p.layersRemoved.connect(self._on_layers_changed)
        p.cleared.connect(self._on_layers_changed)
        self._connected = True

    def _disconnect_project_signals(self):
        if not self._connected:
            return
        p = self._project
        for sig in (p.layersAdded, p.layerWasAdded, p.layersRemoved, p.cleared):
            try:
                sig.disconnect(self._on_layers_changed)
            except Exception:
                pass
        self._connected = False

    def _on_layers_changed(self, *_a, **_k):
        if self.dock and not _is_deleted(self.dock):
            QTimer.singleShot(0, self.dock.refresh_layers)

    def _on_action_toggled(self, checked: bool):
        if self.dock is None:
            self.dock = FieldCalcDock(self.iface)
            self.dock.refresh_layers()
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
            # Deferred: the main window lays out the new dock only after returning to the event loop
            QTimer.singleShot(0, self._fit_dock_height)
            self.dock.visibilityChanged.connect(self._sync_action_with_dock)
            self.dock.destroyed.connect(self._on_dock_destroyed)
            root = QgsProject.instance().layerTreeRoot()
            root.nameChanged.connect(self.dock.refresh_layers, Qt.ConnectionType.QueuedConnection)
            self._lt_name_conn_root = root
            self._lt_name_conn_slot = self.dock.refresh_layers
        self.dock.setVisible(bool(checked))
        if self.toolbar_action and self.toolbar_action.icon().isNull():
            self.toolbar_action.setIcon(_plugin_icon())

    def _fit_dock_height(self):
        """Ask for the natural content height on first show (neighbour docks give up space,
        the window size is the limit); otherwise the dock gets an even split of the dock area
        and the hint text is clipped. The user can resize the dock freely afterwards."""
        if not self.dock or _is_deleted(self.dock) or not self.dock.isVisible() or self.dock.isFloating():
            return
        self.iface.mainWindow().resizeDocks([self.dock], [self.dock.sizeHint().height()], Qt.Orientation.Vertical)

    def _sync_action_with_dock(self, visible: bool):
        # Prevents a toggled→visible→toggled cascade when docks are rearranged
        if self.toolbar_action:
            self.toolbar_action.blockSignals(True)
            try:
                self.toolbar_action.setChecked(bool(visible))
            finally:
                self.toolbar_action.blockSignals(False)

    def _on_dock_destroyed(self, *_args):
        try:
            if getattr(self, "_lt_name_conn_root", None) and getattr(self, "_lt_name_conn_slot", None):
                self._lt_name_conn_root.nameChanged.disconnect(self._lt_name_conn_slot)
        except Exception:
            pass
        self._lt_name_conn_root = None
        self._lt_name_conn_slot = None
        self.dock = None
        if self.toolbar_action:
            self.toolbar_action.setChecked(False)

# --------------- Dock / UI -----------------
class FieldCalcDock(QDockWidget):
    def __init__(self, iface):
        super().__init__()
        self.iface = iface

        self.setWindowTitle(self.tr("Dynamic Field Calculator"))
        self.setObjectName("DynamicFieldCalculatorDock")

        # ► Allow ALL dock widget areas (top/bottom/left/right)
        self.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)

        # Good dock properties (movable, floatable, closable)
        self.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable |
            QDockWidget.DockWidgetFeature.DockWidgetMovable |
            QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )

        self._settings = QgsSettings(ORG)

        # Reaction to dock location change (stabilization for top/bottom)
        self.dockLocationChanged.connect(self._on_dock_location_changed)

        # Main dock widget
        self.main_widget = QWidget()
        self.setWidget(self.main_widget)

        # Outer vertical layout: [functional area] above [footer]
        outer_vbox = QVBoxLayout(self.main_widget)
        outer_vbox.setContentsMargins(0, 0, 0, 0)
        outer_vbox.setSpacing(0)

        # FUNCTIONAL AREA
        self.main_area = QWidget(self.main_widget)
        outer_vbox.addWidget(self.main_area, 1)
        self.main_layout = QHBoxLayout(self.main_area)

        # LEFT COLUMN
        self.left_panel = QWidget()
        self.left_layout = QVBoxLayout(self.left_panel)
        self.left_layout.setContentsMargins(0, 0, 0, 0)
        self.left_layout.setSpacing(6)

        # Layer filter (created before the combo, so Tab goes filter -> combo)
        self.layer_filter = QgsFilterLineEdit()
        self.layer_filter.setShowSearchIcon(True)
        self.layer_filter.setPlaceholderText(self.tr("Filter layers..."))
        self.layer_filter.setToolTip(
            self.tr("Type part of a layer name. Down arrow or Enter opens the list, Esc clears the filter.")
        )
        self.layer_count_label = QLabel()
        self.layer_count_label.setToolTip(self.tr("Layers matching the filter / all vector layers"))

        self.layer_combo = QComboBox()
        # Width must not follow the longest layer name (it would widen the whole dock)
        self.layer_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.layer_combo.setMinimumContentsLength(20)
        self.layer_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layer_label_row = QHBoxLayout()
        layer_label_row.addWidget(QLabel(self.tr("Choose layer:")))
        layer_label_row.addStretch(1)
        layer_label_row.addWidget(self.layer_count_label)
        self.left_layout.addLayout(layer_label_row)
        self.left_layout.addWidget(self.layer_filter)
        self.left_layout.addWidget(self.layer_combo)
        self.layer_combo.currentIndexChanged.connect(self.load_fields)
        self.layer_combo.currentTextChanged.connect(self.layer_combo.setToolTip)
        self.layer_filter.textChanged.connect(self._on_layer_filter_changed)
        self.layer_filter.installEventFilter(self)

        self.field_list = QListWidget()
        self.field_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.left_layout.addWidget(QLabel(self.tr("Select attributes to update:")))
        self.left_layout.addWidget(self.field_list, 1)

        self.only_selected = QCheckBox(self.tr("Selected features only"))
        self.left_layout.addWidget(self.only_selected)

        btn_row = QHBoxLayout()
        self.add_field_button = QPushButton(self.tr("Add selected fields to edit list"))
        self.reset_button = QPushButton(self.tr("Clear panel"))
        btn_row.addWidget(self.add_field_button)
        btn_row.addWidget(self.reset_button)
        self.left_layout.addLayout(btn_row)

        # ---------- Field Sets (named, persistent panel configurations) ----------
        self.fieldsets_group = QGroupBox(self.tr("Field Sets"))
        fs_v = QVBoxLayout(self.fieldsets_group)
        fs_v.setContentsMargins(8, 8, 8, 8)
        fs_v.setSpacing(6)

        self.fieldsets_combo = QComboBox()
        self.fieldsets_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.fieldsets_combo.setMinimumContentsLength(20)
        self.fieldsets_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.fieldsets_combo.currentTextChanged.connect(self.fieldsets_combo.setToolTip)
        fs_v.addWidget(self.fieldsets_combo)

        fs_btn_row = QHBoxLayout()
        self.fieldsets_load_button = QPushButton(self.tr("Load"))
        self.fieldsets_saveas_button = QPushButton(self.tr("Save as..."))
        self.fieldsets_delete_button = QPushButton(self.tr("Delete"))
        fs_btn_row.addWidget(self.fieldsets_load_button)
        fs_btn_row.addWidget(self.fieldsets_saveas_button)
        fs_btn_row.addWidget(self.fieldsets_delete_button)
        fs_v.addLayout(fs_btn_row)

        self.left_layout.addWidget(self.fieldsets_group)

        self.hint = QLabel(
            self.tr(
                "Update multiple attributes in one panel.<br>"
                "Enter expressions using QGIS expression syntax.<br>"
                "You can also click the calculator icon to open the Expression Builder.<br>"
                "<b>NOTE:</b> When updating large datasets, wait until the operation finishes."
            )
        )
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(
            "QLabel{background:#f5f5f7;border:1px solid #d6d6dc;"
            "border-radius:6px;padding:6px;color:#333;}"
        )
        self.left_layout.addWidget(self.hint)

        # RIGHT COLUMN
        self.right_panel = QWidget()
        self.right_layout = QVBoxLayout(self.right_panel)
        self.right_layout.setContentsMargins(0, 0, 0, 0)
        self.right_layout.setSpacing(6)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.expr_container = QWidget()
        self.expression_layout = QVBoxLayout(self.expr_container)
        self.expression_layout.setContentsMargins(6, 6, 6, 6)
        self.expression_layout.setSpacing(6)  # fixed spacing

        # Fixed spacer at the bottom: keeps rows from "floating" when resizing the dock
        self._expr_bottom_spacer = QSpacerItem(0, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)
        self.expression_layout.addItem(self._expr_bottom_spacer)

        self.scroll.setWidget(self.expr_container)
        self.right_layout.addWidget(self.scroll, 1)

        actions_column = QVBoxLayout()
        self.apply_button = QPushButton(self.tr("Apply (no commit)"))
        self.commit_button = QPushButton(self.tr("Commit changes to layer"))
        self.rollback_button = QPushButton(self.tr("Undo last application"))
        self.rollback_button.setToolTip(
            self.tr("Undo only the most recent changes applied by this plugin.")
        )
        self.save_last_button = QPushButton(self.tr("Save field configuration"))
        self.load_last_button = QPushButton(self.tr("Load saved configuration"))
        for b in [self.apply_button, self.commit_button, self.rollback_button,
                  self.save_last_button, self.load_last_button]:
            actions_column.addWidget(b)
        self.right_layout.addLayout(actions_column)

        # MERGE COLUMNS – splitter lets the user resize each column independently.
        # Extra dock width still goes to the right column (stretch 0 / 1).
        self.columns_splitter = QSplitter(Qt.Orientation.Horizontal, self.main_area)
        self.columns_splitter.setObjectName("dkpColumnsSplitter")
        self.columns_splitter.setChildrenCollapsible(False)
        self.columns_splitter.setHandleWidth(7)
        # Thin visible line (the default handle is almost invisible in the Windows style)
        self.columns_splitter.setStyleSheet(
            "QSplitter#dkpColumnsSplitter::handle:horizontal{background-color:palette(mid);margin:8px 3px;}"
            "QSplitter#dkpColumnsSplitter::handle:horizontal:hover{background-color:palette(highlight);}"
        )
        self.columns_splitter.addWidget(self.left_panel)
        self.columns_splitter.addWidget(self.right_panel)
        self.columns_splitter.setStretchFactor(0, 0)
        self.columns_splitter.setStretchFactor(1, 1)
        state = self._settings.value(KEY_SPLITTER_STATE)
        if isinstance(state, QByteArray) and not state.isEmpty():
            self.columns_splitter.restoreState(state)
        self.columns_splitter.splitterMoved.connect(self._save_splitter_state)
        self.main_layout.addWidget(self.columns_splitter)

        self.footer = QLabel("© 2025 Michał Sikora", self.main_widget)
        self.footer.setObjectName("CreditsFooter")
        self.footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.footer.setStyleSheet(
            "QLabel#CreditsFooter{border-top:1px solid palette(mid);padding:8px;}"
        )
        outer_vbox.addWidget(self.footer)

        # DATA MODELS
        self.expression_inputs: Dict[str, QLineEdit] = {}
        self.expression_rows: Dict[str, QWidget] = {}
        self._last_plugin_edit: Optional[Dict] = None

        # SIGNALS
        self.add_field_button.clicked.connect(self.prepare_expression_inputs)
        self.field_list.itemDoubleClicked.connect(self._on_field_double_clicked)
        self.reset_button.clicked.connect(self.reset_ui)
        self.apply_button.clicked.connect(self.apply_changes)
        self.commit_button.clicked.connect(self.commit_changes)
        self.rollback_button.clicked.connect(self.rollback_changes)
        self.save_last_button.clicked.connect(self.save_last_settings)
        self.load_last_button.clicked.connect(self.load_last_settings)

        # Field Sets
        self.fieldsets_load_button.clicked.connect(self.fieldsets_load_selected)
        self.fieldsets_saveas_button.clicked.connect(self.fieldsets_save_as)
        self.fieldsets_delete_button.clicked.connect(self.fieldsets_delete_selected)

        self.refresh_layers()
        self._refresh_fieldsets_combo()

    def _save_splitter_state(self, *_args):
        self._settings.setValue(KEY_SPLITTER_STATE, self.columns_splitter.saveState())

    def _on_dock_location_changed(self, area: Qt.DockWidgetArea):
        """
        Size stabilization when docked at the top/bottom areas.
        It does not limit functionality, but prevents extreme relayouts.
        """
        # size policy: flexible width, preferred height
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        if area in (Qt.DockWidgetArea.TopDockWidgetArea, Qt.DockWidgetArea.BottomDockWidgetArea):
            # reasonable minimum height in horizontal orientation
            self.setMinimumHeight(220)
        else:
            # on the sides: no hard minimum height
            self.setMinimumHeight(0)

    # ---------- Icon for expression builder ----------
    def _expression_button_icon(self) -> QIcon:
        """Returns an icon for the expression builder button – forced SVG file."""
        res = QIcon(CALC_BUTTON_ICON_PATH)
        if not res.isNull():
            return res
        theme = _theme_icon_first(["mIconExpression.svg", "mIconExpressionFunction.svg"])
        if not theme.isNull():
            return theme
        return _draw_text_icon("fx")

    # ---------- Layers / Fields ----------
    @pyqtSlot()
    def refresh_layers(self):
        if _is_deleted(self) or self.widget() is None:
            return
        current = self.current_layer()
        self._populate_layer_combo(current.id() if current else None)
        self.load_fields()

    def _populate_layer_combo(self, current_id: Optional[str]):
        """Fill the layer combo with layers matching the filter.

        The current layer is always kept (even if it does not match), so typing
        in the filter never switches the working layer. Signals stay blocked;
        callers decide whether fields need reloading.
        """
        needle = _fold_text(self.layer_filter.text().strip())
        total = matching = 0

        self.layer_combo.blockSignals(True)
        self.layer_combo.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if not isinstance(layer, QgsVectorLayer):
                continue
            total += 1
            matches = needle in _fold_text(layer.name())
            matching += matches
            if matches or layer.id() == current_id:
                self.layer_combo.addItem(layer.name(), layer.id())

        idx = self.layer_combo.findData(current_id) if current_id else -1
        if idx >= 0:
            self.layer_combo.setCurrentIndex(idx)
        elif self.layer_combo.count() > 0:
            self.layer_combo.setCurrentIndex(0)
        self.layer_combo.blockSignals(False)

        self.layer_count_label.setText(self.tr("{shown} of {total}").format(shown=matching, total=total))
        self._fit_combo_popup(self.layer_combo)

    def _on_layer_filter_changed(self, _text: str = ""):
        current = self.current_layer()
        current_id = current.id() if current else None
        self._populate_layer_combo(current_id)
        # Only possible when no layer was selected before (e.g. the filter matched nothing)
        if self.layer_combo.currentData() != current_id:
            self.load_fields()

    def eventFilter(self, obj, event):
        """Keyboard support in the layer filter: Down/Enter opens the list, Esc clears the filter."""
        if obj is self.layer_filter and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if self.layer_combo.count() > 0:
                    self.layer_combo.setFocus()
                    self.layer_combo.showPopup()
                return True
            if key == Qt.Key.Key_Escape and self.layer_filter.text():
                self.layer_filter.clear()
                return True
        return super().eventFilter(obj, event)

    @staticmethod
    def _fit_combo_popup(combo: QComboBox):
        """Widen the drop-down list to the longest item and show the full current name as a tooltip."""
        view = combo.view()
        # Measured from text: the popup delegate's sizeHint ignores text width in some styles
        fm = view.fontMetrics()
        text_width = max((fm.horizontalAdvance(combo.itemText(i)) for i in range(combo.count())), default=0)
        view.setMinimumWidth(text_width + view.verticalScrollBar().sizeHint().width() + 2 * view.frameWidth() + 16)
        combo.setToolTip(combo.currentText())

    def current_layer(self) -> Optional[QgsVectorLayer]:
        try:
            layer_id = self.layer_combo.currentData()
            if not layer_id:
                return None
            layer = QgsProject.instance().mapLayer(layer_id)
            if isinstance(layer, QgsVectorLayer):
                return layer
        except Exception:
            pass
        return None

    def load_fields(self):
        """Load the list of editable fields of the current layer."""
        self.field_list.clear()
        layer = self.current_layer()
        if not layer:
            return

        fields = layer.fields()
        for f in fields:
            idx = fields.indexFromName(f.name())
            if idx < 0:
                continue
            if fields.fieldOrigin(idx) == _EXPRESSION_FIELD_ORIGIN:
                continue
            try:
                if f.isReadOnly():
                    continue
            except Exception:
                pass
            badge, icon, _pretty = _field_type_info(f)
            item = QListWidgetItem(icon, f.name())
            item.setData(Qt.ItemDataRole.UserRole, (f.name(), badge, _pretty))
            self.field_list.addItem(item)

    # ---------- Creation of editing rows ----------
    def add_expression_input(self, field_name: str):
        """Create an editing row for the given field."""
        if field_name in self.expression_inputs:
            return

        layer = self.current_layer()
        fields = layer.fields() if layer else None
        f = fields.field(field_name) if (fields and fields.indexOf(field_name) != -1) else None
        if f is None:
            badge = "?"
        else:
            badge, _, _ = _field_type_info(f)

        row = QWidget()
        # The row does not grow in height – fixed spacing between rows
        row.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        label = QLabel(f"<b>{field_name}</b> <span style='color:#666;'>[{badge}]</span>")
        label.setTextFormat(Qt.TextFormat.RichText)

        edit = QLineEdit()

        # ► QToolButton with forced icon (SVG) – expression builder
        browse_btn = QToolButton()
        browse_btn.setToolTip(self.tr("Expression Builder"))
        browse_btn.setIcon(self._expression_button_icon())
        # Icon and button size adjusted to the line edit
        h = max(24, edit.sizeHint().height())
        browse_btn.setIconSize(QSize(h - 8, h - 8))
        browse_btn.setFixedSize(QSize(h, h))
        # Subtle frame similar to the previous one
        browse_btn.setStyleSheet(
            "QToolButton{border:1px solid #c9c9cf;border-radius:4px;background:#ffffff;}"
            "QToolButton:hover{background:#f3f6ff;}"
            "QToolButton:pressed{background:#e8eefc;}"
        )

        remove_btn = QPushButton(self.tr("Remove"))
        remove_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        remove_btn.setFixedHeight(h)

        browse_btn.clicked.connect(lambda _, e=edit: self.open_expression_builder(e))
        remove_btn.clicked.connect(lambda _, fname=field_name: self.remove_expression_input(fname))

        lay.addWidget(label)
        lay.addWidget(edit, 1)
        lay.addWidget(browse_btn, 0)
        lay.addWidget(remove_btn, 0)

        # Insert before the bottom spacer so the spacer is always the last item
        idx_sp = self.expression_layout.indexOf(self._expr_bottom_spacer)
        if idx_sp < 0:
            # Safety: add the spacer again if it was removed
            self._expr_bottom_spacer = QSpacerItem(0, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)
            self.expression_layout.addItem(self._expr_bottom_spacer)
            idx_sp = self.expression_layout.indexOf(self._expr_bottom_spacer)
        self.expression_layout.insertWidget(idx_sp, row)

        self.expression_inputs[field_name] = edit
        self.expression_rows[field_name] = row

    def prepare_expression_inputs(self):
        """Add rows for currently selected items in the field list."""
        for i in range(self.field_list.count()):
            item = self.field_list.item(i)
            if not item.isSelected():
                continue
            field_name = item.data(Qt.ItemDataRole.UserRole)[0] if item.data(Qt.ItemDataRole.UserRole) else item.text()
            self.add_expression_input(field_name)

    def _on_field_double_clicked(self, item: QListWidgetItem):
        """Double-click adds just this field (same as the button for a single selected field)."""
        data = item.data(Qt.ItemDataRole.UserRole)
        self.add_expression_input(data[0] if data else item.text())

    def remove_expression_input(self, field_name: str):
        w = self.expression_rows.pop(field_name, None)
        if w:
            w.setParent(None)
            w.deleteLater()
            self.expression_inputs.pop(field_name, None)

    def open_expression_builder(self, target_line_edit: QLineEdit):
        layer = self.current_layer()
        if not layer:
            QMessageBox.information(self, self.tr("No layer"), self.tr("Choose a layer first."))
            return
        dlg = QgsExpressionBuilderDialog(layer, target_line_edit.text(), self)
        if dlg.exec():
            target_line_edit.setText(dlg.expressionText())

    # ---------- Field Sets (persistent named panel configurations) ----------
    def _fieldsets_load_from_settings(self) -> Dict:
        """Load Field Sets JSON from QgsSettings. Returns normalized dict."""
        raw = ""
        try:
            raw = self._settings.value(KEY_FIELDSETS_ROOT, "") or ""
        except Exception:
            raw = ""
        if not raw:
            return {"version": 1, "items": {}}

        try:
            data = json.loads(raw)
        except Exception:
            QMessageBox.warning(
                self,
                self.tr("Data error"),
                self.tr("Cannot read Field Sets data from settings. The stored data may be corrupted.")
            )
            return {"version": 1, "items": {}}

        if not isinstance(data, dict):
            return {"version": 1, "items": {}}

        version = data.get("version", 1)
        items = data.get("items", {})
        if not isinstance(items, dict):
            items = {}

        # Normalize items payloads
        norm_items = {}
        for name, payload in items.items():
            if not isinstance(name, str) or not name.strip():
                continue
            if not isinstance(payload, dict):
                continue
            norm_items[name] = {
                "layer_id": str(payload.get("layer_id", "") or ""),
                "layer_name": str(payload.get("layer_name", "") or ""),
                "selected_only": bool(payload.get("selected_only", False)),
                "fields": list(payload.get("fields", []) or []),
                "expressions": dict(payload.get("expressions", {}) or {}),
            }
        return {"version": int(version) if str(version).isdigit() else 1, "items": norm_items}

    def _fieldsets_save_to_settings(self, data: Dict) -> bool:
        """Save Field Sets JSON to QgsSettings. Returns True on success."""
        try:
            if not isinstance(data, dict):
                return False
            version = data.get("version", 1)
            items = data.get("items", {})
            if not isinstance(items, dict):
                items = {}
            payload = {"version": int(version) if str(version).isdigit() else 1, "items": items}
            self._settings.setValue(KEY_FIELDSETS_ROOT, json.dumps(payload))
            return True
        except Exception:
            QMessageBox.warning(
                self,
                self.tr("Error"),
                self.tr("Failed to save Field Sets to settings.")
            )
            return False

    def _refresh_fieldsets_combo(self):
        """Refresh combo content from settings."""
        data = self._fieldsets_load_from_settings()
        items = data.get("items", {}) or {}

        self.fieldsets_combo.blockSignals(True)
        self.fieldsets_combo.clear()

        names = sorted(items.keys(), key=lambda s: s.lower())

        if not names:
            self.fieldsets_combo.addItem(self.tr("(No Field Sets)"), "")
            self.fieldsets_combo.setEnabled(False)
            self.fieldsets_load_button.setEnabled(False)
            self.fieldsets_delete_button.setEnabled(False)
        else:
            for n in names:
                self.fieldsets_combo.addItem(n, n)
            self.fieldsets_combo.setEnabled(True)
            self.fieldsets_load_button.setEnabled(True)
            self.fieldsets_delete_button.setEnabled(True)

        # Save as... is always enabled (it creates a new set)
        self.fieldsets_saveas_button.setEnabled(True)

        self.fieldsets_combo.blockSignals(False)
        self._fit_combo_popup(self.fieldsets_combo)

    def _collect_panel_state_for_fieldset(self) -> Dict:
        """Collect current panel configuration for a Field Set (data-safe)."""
        fields = list(self.expression_inputs.keys())
        expressions = {f: (w.text() if w else "") for f, w in self.expression_inputs.items()}

        return {
            "selected_only": bool(self.only_selected.isChecked()),
            "fields": fields,
            "expressions": expressions,
        }

    def _apply_fieldset_state_to_panel(self, state: Dict):
        """Apply Field Set configuration to UI (no editing, no apply/commit)."""
        if not isinstance(state, dict):
            QMessageBox.warning(self, self.tr("Data error"), self.tr("Invalid Field Set payload."))
            return

        layer = self.current_layer()
        if not layer:
            QMessageBox.warning(self, self.tr("No layer"), self.tr("Choose a layer first."))
            return

        # Apply stored panel state to the CURRENT layer only (Field Sets are not layer-bound).
        self.reset_ui()

        self.only_selected.setChecked(bool(state.get("selected_only", False)))

        wanted_fields = list(state.get("fields", []) or [])
        exprs = dict(state.get("expressions", {}) or {})

        existing = set()
        try:
            existing = set([f.name() for f in layer.fields()]) if layer else set()
        except Exception:
            existing = set()

        missing = [f for f in wanted_fields if f not in existing]
        load_fields = [f for f in wanted_fields if f in existing]

        for field_name in load_fields:
            self.add_expression_input(field_name)

        for f, t in exprs.items():
            if f in self.expression_inputs:
                try:
                    self.expression_inputs[f].setText(str(t) if t is not None else "")
                except Exception:
                    pass

        if missing:
            QMessageBox.warning(
                self,
                self.tr("Missing fields"),
                self.tr("Some fields from the Field Set are not present in the current layer and were skipped:\n\n{lst}").format(
                    lst="\n".join(missing)
                )
            )

    def fieldsets_load_selected(self):
        name = self.fieldsets_combo.currentData()
        if not name:
            return
        data = self._fieldsets_load_from_settings()
        items = data.get("items", {}) or {}
        state = items.get(name)
        if not state:
            QMessageBox.warning(self, self.tr("Not found"), self.tr("Selected Field Set was not found."))
            self._refresh_fieldsets_combo()
            return
        self._apply_fieldset_state_to_panel(state)

    def fieldsets_save_as(self):
        state = self._collect_panel_state_for_fieldset()
        if not self.current_layer():
            QMessageBox.information(
                self,
                self.tr("No layer"),
                self.tr("Choose a layer before saving a Field Set.")
            )
            return

        name, ok = QInputDialog.getText(self, self.tr("Save Field Set"), self.tr("Field Set name:"))
        if not ok:
            return
        name = (name or "").strip()
        if not name:
            QMessageBox.warning(self, self.tr("Invalid name"), self.tr("Field Set name cannot be empty."))
            return

        data = self._fieldsets_load_from_settings()
        items = data.get("items", {}) or {}

        if name in items:
            res = QMessageBox.question(
                self,
                self.tr("Overwrite Field Set"),
                self.tr("A Field Set with this name already exists. Overwrite?"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if res != QMessageBox.StandardButton.Yes:
                return

        items[name] = state
        data["items"] = items
        if self._fieldsets_save_to_settings(data):
            self._refresh_fieldsets_combo()
            idx = self.fieldsets_combo.findData(name)
            if idx >= 0:
                self.fieldsets_combo.setCurrentIndex(idx)

    def fieldsets_delete_selected(self):
        name = self.fieldsets_combo.currentData()
        if not name:
            return
        res = QMessageBox.question(
            self,
            self.tr("Delete Field Set"),
            self.tr("Delete Field Set '{name}'?").format(name=name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if res != QMessageBox.StandardButton.Yes:
            return

        data = self._fieldsets_load_from_settings()
        items = data.get("items", {}) or {}
        if name in items:
            items.pop(name, None)
            data["items"] = items
            self._fieldsets_save_to_settings(data)

        self._refresh_fieldsets_combo()

    # ---------- Operations ----------
    def _ensure_editable_or_prompt(self, layer: QgsVectorLayer) -> bool:
        """Ensure layer is editable, asking the user if edit mode should be enabled."""
        if not layer:
            return False
        if layer.isEditable():
            return True

        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setWindowTitle(self.tr("Edit mode"))
        msg.setText(self.tr("The layer edit mode is disabled. Enable edit mode and apply the changes?"))

        btn_accept = msg.addButton(self.tr("Accept"), QMessageBox.ButtonRole.AcceptRole)
        msg.addButton(self.tr("Discard"), QMessageBox.ButtonRole.RejectRole)
        msg.setDefaultButton(btn_accept)
        msg.exec()

        if msg.clickedButton() != btn_accept:
            return False

        if not layer.startEditing():
            # Use existing messaging style for failure
            QMessageBox.warning(self, self.tr("Error"), self.tr("Cannot start editing this layer."))
            return False

        return True

    def apply_changes(self):
        from qgis.PyQt.QtWidgets import QProgressDialog
        from qgis.PyQt.QtCore import Qt

        layer = self.current_layer()
        if not layer:
            QMessageBox.warning(self, self.tr("No layer"), self.tr("Choose a vector layer."))
            return
        if not self.expression_inputs:
            QMessageBox.information(self, self.tr("No fields"), self.tr("Add at least one field to edit."))
            return

        # Provider capabilities
        caps = layer.dataProvider().capabilities()
        if not (caps & QgsVectorDataProvider.Capability.ChangeAttributeValues):
            QMessageBox.warning(self, self.tr("No permissions"), self.tr("This layer does not support attribute editing."))
            return

        only_selected = self.only_selected.isChecked()

        # Prepare and validate expressions + dependency analysis
        expressions: Dict[str, QgsExpression] = {}
        field_index: Dict[str, int] = {}

        ctx = QgsExpressionContext()
        ctx.appendScope(QgsExpressionContextUtils.globalScope())
        ctx.appendScope(QgsExpressionContextUtils.projectScope(QgsProject.instance()))
        ctx.appendScope(QgsExpressionContextUtils.layerScope(layer))

        for field, w in self.expression_inputs.items():
            text = (w.text() or "").strip()
            if not text:
                continue
            exp = QgsExpression(text)
            if exp.hasParserError():
                QMessageBox.critical(
                    self,
                    self.tr("Expression error"),
                    self.tr("[{field}] {err}").format(field=field, err=exp.parserErrorString())
                )
                return
            if not exp.prepare(ctx):
                err = exp.evalErrorString() or self.tr("Expression could not be prepared.")
                QMessageBox.critical(
                    self,
                    self.tr("Expression error"),
                    self.tr("[{field}] {err}").format(field=field, err=err)
                )
                return

            expressions[field] = exp
            idx = layer.fields().indexFromName(field)
            if idx < 0:
                QMessageBox.critical(
                    self,
                    self.tr("Missing fields"),
                    self.tr("Field {field} is no longer available.").format(field=field)
                )
                return
            field_index[field] = idx

        if not expressions:
            QMessageBox.information(self, self.tr("No expressions"), self.tr("Fill in expressions for the selected fields."))
            return

        needed_attrs, requires_all_attributes, needs_geom = _expression_dependencies(
            expressions.values()
        )

        # Build feature request: only necessary data
        req = QgsFeatureRequest()
        selected_fids = None
        if only_selected:
            selected_fids = list(layer.selectedFeatureIds())
            if selected_fids:
                req.setFilterFids(selected_fids)
            else:
                QMessageBox.information(self, self.tr("No selection"), self.tr("No features are selected."))
                return

        if not needs_geom:
            req.setFlags(QgsFeatureRequest.Flag.NoGeometry)

        # ALL_ATTRIBUTES means that functions such as attribute(), attributes()
        # and eval() need the complete feature.  In that case do not set a
        # subset at all -- an empty subset would silently evaluate to NULL on
        # providers which honor the request (e.g. GeoPackage/OGR).
        #
        # Target fields are also required even when expressions do not refer
        # to them.  Their current values are passed to changeAttributeValue()
        # as oldValue, so QGIS can undo or roll back the command without
        # fetching every original value from the provider again.
        if not requires_all_attributes:
            idxs = set(field_index.values())
            idxs.update(
                layer.fields().indexFromName(name)
                for name in needed_attrs
                if layer.fields().indexFromName(name) != -1
            )
            req.setSubsetOfAttributes(sorted(idxs))

        # Validation is complete.  Only now may the operation start or enable
        # an edit session on the layer.
        if not self._ensure_editable_or_prompt(layer):
            return

        # ========== PROGRESS & CANCEL ==========
        # Count total features BEFORE starting edit command
        total_features = len(selected_fids) if selected_fids is not None else layer.featureCount()

        # Create progress dialog with Cancel button
        progress = QProgressDialog(
            self.tr("Processing features..."),
            self.tr("Cancel"),
            0,
            total_features,
            self
        )
        progress.setWindowModality(Qt.WindowModality.WindowModal)  # Block interaction with dock
        progress.setMinimumDuration(0)  # Show immediately
        progress.setAutoClose(True)
        progress.setAutoReset(False)

        total_changes = 0
        processed = 0
        update_interval = 100  # Update UI every 100 features (balance between responsiveness and performance)

        try:
            # NOTE: We intentionally do NOT block layer/edit-buffer signals here.
            # Blocking signals prevents the Attribute Table model from receiving change notifications,
            # so the table would not refresh until reopened.
            #
            # For performance (map rendering), we temporarily disable canvas rendering.
            canvas = None
            prev_render_flag = None
            try:
                canvas = self.iface.mapCanvas() if hasattr(self.iface, "mapCanvas") else None
                if canvas is not None:
                    prev_render_flag = canvas.renderFlag()
                    canvas.setRenderFlag(False)
            except Exception:
                canvas = None
                prev_render_flag = None

            # Visible in undo stack.  The indexes let us later undo only this
            # command, without rolling back unrelated edits in the layer.
            undo_stack = layer.undoStack()
            undo_index_before = undo_stack.index()
            command_text = self.tr("DKP: apply expressions (preview)")
            layer.beginEditCommand(command_text)

            for ftr in layer.getFeatures(req):
                # CHECK CANCEL every iteration (fast operation)
                if progress.wasCanceled():
                    raise UserWarning(self.tr("Operation cancelled by user"))

                ctx.setFeature(ftr)
                for field_name, exp in expressions.items():
                    idx = field_index.get(field_name, -1)
                    if idx < 0:
                        continue
                    val = exp.evaluate(ctx)
                    if exp.hasEvalError():
                        raise RuntimeError(f"[{field_name}] {exp.evalErrorString()}")

                    # Match the result type to the field type
                    fld = layer.fields()[idx]
                    if _is_complex_field(fld):
                        ok_val, complex_val = _to_complex_value(val, fld)
                        if not ok_val:
                            if fld.type() == QMetaType.Type.QVariantMap and not _is_json_field(fld):
                                # map/hstore accepts only objects
                                err = self.tr("Field {field} requires an object value, e.g. map('key','value'). Got: {value}")
                            else:
                                err = self.tr("Field {field} requires a list or JSON value, e.g. array('B','R'). Got: {value}")
                            raise RuntimeError(err.format(field=field_name, value=str(val)[:80]))
                        val = complex_val
                    else:
                        try:
                            val = _coerce_to_field_type(val, fld)
                        except Exception:
                            pass

                    old_value = ftr.attribute(idx)
                    ok = layer.changeAttributeValue(
                        ftr.id(),
                        idx,
                        val,
                        old_value,
                    )
                    if not ok:
                        raise RuntimeError(
                            self.tr("Failed to change {field} for feature FID={fid}").format(
                                field=field_name, fid=ftr.id()
                            )
                        )
                    total_changes += 1

                processed += 1

                # UPDATE PROGRESS periodically (not every iteration to avoid UI lag)
                if processed % update_interval == 0:
                    progress.setValue(processed)
                    QgsApplication.processEvents()  # Keep UI responsive

            # Final progress update
            progress.setValue(total_features)

            # SUCCESS: commit the edit command to undo stack
            layer.endEditCommand()
            undo_index_after = undo_stack.index()
            if undo_index_after == undo_index_before + 1:
                self._last_plugin_edit = {
                    "layer_id": layer.id(),
                    "before_index": undo_index_before,
                    "after_index": undo_index_after,
                }
            else:
                self._last_plugin_edit = None

        except UserWarning as e:
            # USER CANCELLED: destroy edit command (no undo entry, no changes)
            try:
                layer.destroyEditCommand()
            except Exception:
                pass
            progress.close()
            self.iface.messageBar().pushMessage(
                self.tr("Cancelled"),
                str(e),
                level=Qgis.MessageLevel.Warning,
                duration=5
            )
            return

        except Exception as e:
            # ERROR: destroy edit command (no undo entry, no changes)
            try:
                layer.destroyEditCommand()
            except Exception:
                pass
            progress.close()
            QMessageBox.critical(
                self,
                self.tr("Error"),
                self.tr("Operation aborted: {err}").format(err=e)
            )
            return

        finally:
            # Always restore map rendering (even on cancel/error)
            try:
                if 'canvas' in locals() and canvas is not None and prev_render_flag is not None:
                    canvas.setRenderFlag(prev_render_flag)
                    canvas.refresh()
            except Exception:
                pass

            progress.close()

        try:
            layer.dataChanged.emit()  # refresh Attribute Table model (safe even if redundant)
        except Exception:
            pass
        layer.triggerRepaint()

        # SUCCESS MESSAGE via message bar (non-blocking)
        self.iface.messageBar().pushMessage(
            self.tr("Done"),
            self.tr("Expressions applied to {n} values. Remember to commit changes.").format(n=total_changes),
            level=Qgis.MessageLevel.Success,
            duration=8
        )


    def commit_changes(self):
        layer = self.current_layer()
        if not layer:
            return
        if not layer.isEditable():
            QMessageBox.information(
                self,
                self.tr("No changes"),
                self.tr("The layer is not in edit mode and has no changes to commit.")
            )
            return

        res = QMessageBox.question(
            self,
            self.tr("Commit layer changes"),
            self.tr(
                "This will commit all uncommitted changes in the layer, "
                "including changes made outside this plugin. Continue?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if res != QMessageBox.StandardButton.Yes:
            return

        if layer.commitChanges():
            self._last_plugin_edit = None
            QMessageBox.information(self, self.tr("Committed"), self.tr("Changes committed to the layer (editing stopped)."))
        else:
            errs = layer.commitErrors() or []
            msg = self.tr("Commit failed. The layer remains in edit mode and its changes were preserved.")
            if errs:
                msg += self.tr("\n\nDetails:\n- ") + "\n- ".join(errs)
            QMessageBox.critical(self, self.tr("Error"), msg)

    def rollback_changes(self):
        layer = self.current_layer()
        if not layer or not layer.isEditable():
            QMessageBox.information(
                self,
                self.tr("No changes"),
                self.tr("There is no active plugin application to undo.")
            )
            return

        state = self._last_plugin_edit or {}
        undo_stack = layer.undoStack()
        can_undo_safely = (
            state.get("layer_id") == layer.id()
            and undo_stack.canUndo()
            and undo_stack.index() == state.get("after_index")
            and state.get("after_index") == state.get("before_index", -1) + 1
        )
        if not can_undo_safely:
            QMessageBox.warning(
                self,
                self.tr("Cannot undo safely"),
                self.tr(
                    "The plugin changes are no longer the most recent edit. "
                    "Use the QGIS undo history to choose which changes to undo."
                )
            )
            return

        undo_stack.undo()
        self._last_plugin_edit = None
        try:
            layer.dataChanged.emit()
        except Exception:
            pass
        layer.triggerRepaint()
        QMessageBox.information(
            self,
            self.tr("Undone"),
            self.tr("The last changes applied by this plugin were undone. Other layer edits were preserved.")
        )

    # ---------- Save / Load ----------
    def save_last_settings(self):
        layer = self.current_layer()
        data = {
            "version": 2,
            "layer_id": layer.id() if layer else "",
            "layer_name": layer.name() if layer else "",
            "only_selected": self.only_selected.isChecked(),
            "expressions": {f: w.text() for f, w in self.expression_inputs.items()},
        }
        self._settings.setValue(KEY_LAST, json.dumps(data))
        QMessageBox.information(self, self.tr("Saved"), self.tr("Current settings were saved as the last configuration."))

    def load_last_settings(self):
        raw = self._settings.value(KEY_LAST, "")
        if not raw:
            QMessageBox.information(self, self.tr("No data"), self.tr("No saved settings found yet."))
            return
        try:
            data = json.loads(raw)
        except Exception:
            QMessageBox.warning(self, self.tr("Data error"), self.tr("Cannot read the saved settings."))
            return

        if not isinstance(data, dict):
            QMessageBox.warning(self, self.tr("Data error"), self.tr("Cannot read the saved settings."))
            return

        target_id = str(data.get("layer_id", "") or "")
        target_name = str(data.get("layer_name", "") or "")
        target_index = -1

        if target_id or target_name:
            # The saved layer may currently be hidden by the filter.
            self.layer_filter.clear()
            if target_id:
                target_index = self.layer_combo.findData(target_id)

            if target_index < 0 and target_name:
                name_matches = [
                    i for i in range(self.layer_combo.count())
                    if self.layer_combo.itemText(i) == target_name
                ]
                if len(name_matches) == 1:
                    target_index = name_matches[0]
                elif len(name_matches) > 1:
                    QMessageBox.warning(
                        self,
                        self.tr("Ambiguous layer"),
                        self.tr(
                            "More than one layer is named '{name}'. "
                            "Choose the target layer manually before loading the configuration."
                        ).format(name=target_name)
                    )
                    return

            if target_index < 0:
                QMessageBox.warning(
                    self,
                    self.tr("Layer not found"),
                    self.tr("The layer saved with this configuration is not available in the project.")
                )
                return

        self.reset_ui()
        if target_index >= 0:
            self.layer_combo.setCurrentIndex(target_index)

        self.only_selected.setChecked(bool(data.get("only_selected", False)))
        exprs = data.get("expressions", {}) or {}

        for field_name in exprs.keys():
            self.add_expression_input(field_name)

        for f, t in exprs.items():
            if f in self.expression_inputs:
                self.expression_inputs[f].setText(t)

    def reset_ui(self):
        for w in list(self.expression_rows.values()):
            w.setParent(None)
            w.deleteLater()
        self.expression_rows.clear()
        self.expression_inputs.clear()

        # Restore the bottom spacer (in case it was removed)
        while True:
            idx = self.expression_layout.indexOf(self._expr_bottom_spacer)
            if idx == -1:
                break
            self.expression_layout.takeAt(idx)
        self._expr_bottom_spacer = QSpacerItem(0, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)
        self.expression_layout.addItem(self._expr_bottom_spacer)

        self.field_list.clearSelection()
        self.load_fields()
