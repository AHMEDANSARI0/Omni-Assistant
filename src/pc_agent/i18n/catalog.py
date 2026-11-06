"""Translation catalogs for the Transformation Phase 17 interface languages.

Every value here is a real translation written for this product, not a
placeholder and not a copy of the English text (the test suite refuses both:
a locale whose value equals the English string for a key that is not marked as
``identical_ok`` fails).

Only *interface* strings live here. Anything that the assistant prints about
your data (task instructions, reminder titles, file paths, provider errors)
stays exactly as it is - automatically "translating" user content would be
inventing information.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "DEFAULT_LOCALE",
    "DIRECTIONS",
    "IDENTICAL_OK",
    "LOCALES",
    "LOCALE_NAMES",
    "MESSAGES",
]

DEFAULT_LOCALE = "en"

#: Locale -> writing direction. `rtl` locales mirror layouts and set alignment.
DIRECTIONS: dict[str, str] = {
    "en": "ltr",
    "es": "ltr",
    "ur": "rtl",
    "ar": "rtl",
}

#: Locale -> the name shown in the language picker, written *in that language*.
LOCALE_NAMES: dict[str, str] = {
    "en": "English",
    "es": "Español",
    "ur": "اردو",
    "ar": "العربية",
}

#: Proper nouns, product names and keys that are legitimately identical in every
#: language. Kept deliberately tiny so the "no fake translation" check stays strict.
IDENTICAL_OK: frozenset[str] = frozenset(
    {
        "app.name",
        "app.version",
        "unit.seconds",
        "unit.ms",
        "unit.percent",
    }
)

LOCALES: tuple[str, ...] = ("en", "es", "ur", "ar")


#: The English catalog is the source of truth: it defines the key set and the
#: placeholders every other locale must reproduce.
MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "app.name": "Omni Assistant",
        "app.version": "Version",
        "nav.dashboard": "Dashboard",
        "nav.missions": "Missions",
        "nav.approvals": "Approvals",
        "nav.memory": "Memory",
        "nav.agents": "Agents",
        "nav.knowledge": "Knowledge",
        "nav.flows": "Workflows",
        "nav.connectors": "Connectors",
        "nav.proactive": "Proactive",
        "nav.plugins": "Plugins",
        "nav.sync": "Sync",
        "nav.accessibility": "Accessibility",
        "nav.settings": "Settings",
        "action.refresh": "Refresh",
        "action.cancel": "Cancel",
        "action.save": "Save",
        "action.close": "Close",
        "action.export": "Export",
        "action.import": "Import",
        "action.accept": "Accept",
        "action.deny": "Deny",
        "action.retry": "Retry",
        "action.stop": "Stop",
        "action.test": "Test",
        "state.on": "on",
        "state.off": "off",
        "state.ready": "ready",
        "state.loading": "loading",
        "state.failed": "failed",
        "state.pending": "pending",
        "state.never": "never",
        "state.unavailable": "unavailable",
        "label.status": "Status",
        "label.language": "Language",
        "label.direction": "Text direction",
        "label.text_scale": "Text size",
        "label.high_contrast": "High contrast",
        "label.reduce_motion": "Reduce motion",
        "label.keyboard_shortcuts": "Keyboard shortcuts",
        "label.coverage": "Translation coverage",
        "label.limitations": "Known limits",
        "label.tab": "Tab",
        "label.tab_index": "Tab {index}",
        "msg.language_changed": "Language set to {name}.",
        "msg.scale_applied": "Text size is now {percent}%.",
        "msg.contrast_on": "High contrast is on.",
        "msg.contrast_off": "High contrast is off.",
        "msg.motion_reduced": "Reduced motion is on.",
        "msg.motion_normal": "Reduced motion is off.",
        "msg.rtl_notice": (
            "This language reads right to left; the layout is mirrored."
        ),
        "msg.fallback_notice": (
            "{count} text(s) are shown in English because the translation is "
            "not written yet."
        ),
        "a11y.focus_order": "Focus moves through every control with Tab.",
        "a11y.escape": "Escape closes the active dialog and cancels edits.",
        "a11y.shortcuts": "Alt+1..9 jumps to a tab, Ctrl+R refreshes live data.",
        "a11y.no_trap": "No keyboard trap: every region can be left with Tab.",
        "a11y.no_timing": "No screen changes on a timer you cannot pause.",
        "unit.seconds": "seconds",
        "unit.ms": "ms",
        "unit.percent": "%",
    },
    "es": {
        "app.name": "Omni Assistant",
        "app.version": "Versión",
        "nav.dashboard": "Panel",
        "nav.missions": "Misiones",
        "nav.approvals": "Aprobaciones",
        "nav.memory": "Memoria",
        "nav.agents": "Agentes",
        "nav.knowledge": "Conocimiento",
        "nav.flows": "Flujos de trabajo",
        "nav.connectors": "Conectores",
        "nav.proactive": "Proactivo",
        "nav.plugins": "Complementos",
        "nav.sync": "Sincronización",
        "nav.accessibility": "Accesibilidad",
        "nav.settings": "Ajustes",
        "action.refresh": "Actualizar",
        "action.cancel": "Cancelar",
        "action.save": "Guardar",
        "action.close": "Cerrar",
        "action.export": "Exportar",
        "action.import": "Importar",
        "action.accept": "Aceptar",
        "action.deny": "Rechazar",
        "action.retry": "Reintentar",
        "action.stop": "Detener",
        "action.test": "Probar",
        "state.on": "activo",
        "state.off": "inactivo",
        "state.ready": "listo",
        "state.loading": "cargando",
        "state.failed": "fallido",
        "state.pending": "pendiente",
        "state.never": "nunca",
        "state.unavailable": "no disponible",
        "label.status": "Estado",
        "label.language": "Idioma",
        "label.direction": "Dirección del texto",
        "label.text_scale": "Tamaño del texto",
        "label.high_contrast": "Alto contraste",
        "label.reduce_motion": "Reducir movimiento",
        "label.keyboard_shortcuts": "Atajos de teclado",
        "label.coverage": "Cobertura de traducción",
        "label.limitations": "Límites conocidos",
        "label.tab": "Pestaña",
        "label.tab_index": "Pestaña {index}",
        "msg.language_changed": "Idioma cambiado a {name}.",
        "msg.scale_applied": "El texto ahora está al {percent}%.",
        "msg.contrast_on": "El alto contraste está activado.",
        "msg.contrast_off": "El alto contraste está desactivado.",
        "msg.motion_reduced": "El movimiento reducido está activado.",
        "msg.motion_normal": "El movimiento reducido está desactivado.",
        "msg.rtl_notice": "Este idioma se lee de izquierda a derecha.",
        "msg.fallback_notice": (
            "{count} texto(s) se muestran en inglés porque la traducción aún no "
            "está escrita."
        ),
        "a11y.focus_order": "El foco recorre cada control con Tab.",
        "a11y.escape": "Escape cierra el diálogo activo y cancela los cambios.",
        "a11y.shortcuts": "Alt+1..9 salta a una pestaña, Ctrl+R actualiza los datos.",
        "a11y.no_trap": "Sin trampa de teclado: se puede salir con Tab.",
        "a11y.no_timing": "Nada cambia en pantalla con un temporizador que no puedas pausar.",
        "unit.seconds": "segundos",
        "unit.ms": "ms",
        "unit.percent": "%",
    },
    "ur": {
        "app.name": "Omni Assistant",
        "app.version": "ورژن",
        "nav.dashboard": "ڈیش بورڈ",
        "nav.missions": "مشن",
        "nav.approvals": "منظوریاں",
        "nav.memory": "یادداشت",
        "nav.agents": "ایجنٹس",
        "nav.knowledge": "علم",
        "nav.flows": "ورک فلو",
        "nav.connectors": "کنیکٹرز",
        "nav.proactive": "پیشگی",
        "nav.plugins": "پلگ اِنز",
        "nav.sync": "ہم آہنگی",
        "nav.accessibility": "رسائی",
        "nav.settings": "ترتیبات",
        "action.refresh": "تازہ کریں",
        "action.cancel": "منسوخ",
        "action.save": "محفوظ کریں",
        "action.close": "بند کریں",
        "action.export": "برآمد کریں",
        "action.import": "درآمد کریں",
        "action.accept": "قبول کریں",
        "action.deny": "انکار کریں",
        "action.retry": "دوبارہ کوشش",
        "action.stop": "روکیں",
        "action.test": "جانچیں",
        "state.on": "فعال",
        "state.off": "غیر فعال",
        "state.ready": "تیار",
        "state.loading": "لوڈ ہو رہا ہے",
        "state.failed": "ناکام",
        "state.pending": "زیر التوا",
        "state.never": "کبھی نہیں",
        "state.unavailable": "دستیاب نہیں",
        "label.status": "حالت",
        "label.language": "زبان",
        "label.direction": "متن کی سمت",
        "label.text_scale": "متن کا سائز",
        "label.high_contrast": "زیادہ تضاد",
        "label.reduce_motion": "حرکت کم کریں",
        "label.keyboard_shortcuts": "کی بورڈ شارٹ کٹ",
        "label.coverage": "ترجمہ کی مکمل شرح",
        "label.limitations": "معلوم حدود",
        "label.tab": "ٹیب",
        "label.tab_index": "ٹیب {index}",
        "msg.language_changed": "زبان {name} مقرر ہو گئی۔",
        "msg.scale_applied": "متن کا سائز اب {percent}% ہے۔",
        "msg.contrast_on": "زیادہ تضاد فعال ہے۔",
        "msg.contrast_off": "زیادہ تضاد غیر فعال ہے۔",
        "msg.motion_reduced": "حرکت کم کرنے کی سہولت فعال ہے۔",
        "msg.motion_normal": "حرکت کم کرنے کی سہولت غیر فعال ہے۔",
        "msg.rtl_notice": "یہ زبان دائیں سے بائیں پڑھی جاتی ہے؛ ترتیب دائیں سے بائیں ہے۔",
        "msg.fallback_notice": (
            "{count} متن انگریزی میں دکھایا گیا ہے کیونکہ ترجمہ ابھی نہیں لکھا گیا۔"
        ),
        "a11y.focus_order": "ٹیب دبانے سے فوکس ہر کنٹرول پر جاتا ہے۔",
        "a11y.escape": "ایسکیپ فعال ڈائیلاگ بند کرتا ہے اور تبدیلیاں منسوخ کرتا ہے۔",
        "a11y.shortcuts": "Alt+1..9 سے ٹیب پر جائیں، Ctrl+R سے ڈیٹا تازہ کریں۔",
        "a11y.no_trap": "کی بورڈ پھنساؤ نہیں: ٹیب سے ہر جگہ سے نکل سکتے ہیں۔",
        "a11y.no_timing": "ایسا کوئی ٹائمر نہیں جو آپ کی اجازت کے بغیر اسکرین بدلے۔",
        "unit.seconds": "سیکنڈ",
        "unit.ms": "ملی سیکنڈ",
        "unit.percent": "%",
    },
    "ar": {
        "app.name": "Omni Assistant",
        "app.version": "الإصدار",
        "nav.dashboard": "لوحة التحكم",
        "nav.missions": "المهام",
        "nav.approvals": "الموافقات",
        "nav.memory": "الذاكرة",
        "nav.agents": "الوكلاء",
        "nav.knowledge": "المعرفة",
        "nav.flows": "سير العمل",
        "nav.connectors": "الموصّلات",
        "nav.proactive": "استباقي",
        "nav.plugins": "الإضافات",
        "nav.sync": "المزامنة",
        "nav.accessibility": "إمكانية الوصول",
        "nav.settings": "الإعدادات",
        "action.refresh": "تحديث",
        "action.cancel": "إلغاء",
        "action.save": "حفظ",
        "action.close": "إغلاق",
        "action.export": "تصدير",
        "action.import": "استيراد",
        "action.accept": "قبول",
        "action.deny": "رفض",
        "action.retry": "إعادة المحاولة",
        "action.stop": "إيقاف",
        "action.test": "اختبار",
        "state.on": "مُفعّل",
        "state.off": "غير مُفعّل",
        "state.ready": "جاهز",
        "state.loading": "جارٍ التحميل",
        "state.failed": "فشل",
        "state.pending": "قيد الانتظار",
        "state.never": "أبدًا",
        "state.unavailable": "غير متاح",
        "label.status": "الحالة",
        "label.language": "اللغة",
        "label.direction": "اتجاه النص",
        "label.text_scale": "حجم النص",
        "label.high_contrast": "تباين عالٍ",
        "label.reduce_motion": "تقليل الحركة",
        "label.keyboard_shortcuts": "اختصارات لوحة المفاتيح",
        "label.coverage": "نسبة الترجمة",
        "label.limitations": "الحدود المعروفة",
        "label.tab": "تبويب",
        "label.tab_index": "تبويب {index}",
        "msg.language_changed": "تم تعيين اللغة إلى {name}.",
        "msg.scale_applied": "حجم النص الآن {percent}%.",
        "msg.contrast_on": "التباين العالي مُفعّل.",
        "msg.contrast_off": "التباين العالي غير مُفعّل.",
        "msg.motion_reduced": "تقليل الحركة مُفعّل.",
        "msg.motion_normal": "تقليل الحركة غير مُفعّل.",
        "msg.rtl_notice": "تُقرأ هذه اللغة من اليمين إلى اليسار؛ التخطيط معكوس.",
        "msg.fallback_notice": (
            "{count} نصًا يظهر بالإنجليزية لأن الترجمة لم تُكتب بعد."
        ),
        "a11y.focus_order": "ينتقل التركيز بين كل العناصر بمفتاح Tab.",
        "a11y.escape": "مفتاح Escape يغلق النافذة النشطة ويلغي التعديلات.",
        "a11y.shortcuts": "Alt+1..9 ينقلك إلى تبويب، وCtrl+R يحدّث البيانات.",
        "a11y.no_trap": "لا يوجد احتجاز للتركيز: يمكن الخروج بمفتاح Tab.",
        "a11y.no_timing": "لا يتغير شيء على الشاشة بمؤقت لا يمكنك إيقافه.",
        "unit.seconds": "ثانية",
        "unit.ms": "مللي ثانية",
        "unit.percent": "%",
    },
}


def catalog_for(locale: str) -> dict[str, str]:
    """Return the catalog for ``locale`` (English when unknown)."""

    return MESSAGES.get(str(locale or "").strip().lower(), MESSAGES[DEFAULT_LOCALE])


def key_set(locale: str) -> set[str]:
    return set(catalog_for(locale))


def placeholder_names(value: str) -> set[str]:
    """Extract ``{name}`` placeholders so translators cannot drop or invent them."""

    names: set[str] = set()
    depth = 0
    buffer: list[str] = []
    for character in value:
        if character == "{":
            depth += 1
            buffer = []
            continue
        if character == "}":
            if depth and buffer:
                names.add("".join(buffer).strip())
            depth = 0
            buffer = []
            continue
        if depth:
            buffer.append(character)
    return names


def coverage(locale: str) -> dict[str, Any]:
    """Real, computed coverage: how many English keys this locale really has."""

    source = MESSAGES[DEFAULT_LOCALE]
    target = catalog_for(locale)
    missing = sorted(key for key in source if key not in target)
    present = len(source) - len(missing)
    percent = 100.0 if not source else round(present * 100.0 / len(source), 1)
    return {
        "locale": str(locale),
        "total_keys": len(source),
        "translated_keys": present,
        "missing_keys": missing,
        "percent": percent,
        "direction": DIRECTIONS.get(str(locale), "ltr"),
    }
