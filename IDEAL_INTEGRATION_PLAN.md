# Plan: docelowa integracja Grenton dla Home Assistant

Dokument projektowy. Łączy mocne strony dwóch istniejących integracji:

- natywnej (`homeassistant_grenton`, szyfrowany UDP, auto-discovery, push),
- HTTP (`grenton_objects`, jawna semantyka encji, testy, szeroki zakres urządzeń).

Cel: jeden rdzeń discovery + push, wzbogacony o realne typy obiektów Grentona,
żeby odzyskać semantykę, której interfejs mobilny nie wystawia, i wyeliminować
obejścia (syntetyczny biały kanał, helper threshold, switch_as_x, ręczne device_class).

## 1. Zasady projektowe

1. Zero ręcznej konfiguracji per obiekt. Jeden wpis konfiguracyjny na instalację.
2. Discovery z natywnego protokołu + wzbogacenie metadanymi typów obiektów z Object Managera.
3. Semantyka wyprowadzana z typu obiektu Grentona, nie z widgetu UI.
4. Push (subskrypcje) jako podstawa; polling tylko jako fallback.
5. Typowany model komend zamiast sklejania stringów.
6. Deklaratywne mapowanie typ-obiektu na encję (wzorzec EntityDescription).
7. Każda warstwa testowalna w izolacji; brak zależności od HA w warstwie domenowej.

## 2. Architektura warstwowa

```
transport/      surowy szyfrowany UDP, sesje, ping, fan-out raportów subskrypcji
  protocol.py        kodowanie/dekodowanie ramek
  cipher.py          szyfrowanie (AES), klucze sesji
  session.py         jedna sesja per CLU, keepalive
  subscription.py    rejestracja kluczy (chunki <= 30), routing raportów

discovery/      skąd biorą się obiekty
  interface.py       pobranie interfejsu mobilnego z Object Managera
  object_manager.py  metadane typów obiektów (DIN/DOUT/DIMM/LED/ROL/ANALOG...)
  catalog.py         scalony katalog: obiekt + typ + cechy + akcje + stany

domain/         logika niezależna od HA (czysty Python, testowalna)
  objects.py         model obiektu Grentona (id, clu, typ, cechy)
  command.py         typowany model komendy (object, method_index, args[])
  feature.py         cecha obiektu (index, kierunek, zakres, jednostka)
  value.py           kasty/parsowanie wartości

coordinator.py  most domena <-> HA: trzyma stan, subskrypcje, kolejkę wysyłki

entities/       platformy HA + deklaratywne opisy
  descriptions.py    mapowanie GrentonObjectType -> EntityDescription
  light.py, switch.py, sensor.py, binary_sensor.py, cover.py, climate.py, event.py, button.py

config_flow.py  setup (IP/port/PIN), reconfigure, opcje per-encja (override typu)
diagnostics.py  zrzut diagnostyczny (zredagowany)
manifest.json   iot_class: local_push, quality_scale, integration_type: hub
```

Zasada zależności: `transport` i `domain` nie importują `homeassistant`.
`coordinator` i `entities` to jedyna warstwa zależna od HA. Ułatwia to testy.

## 3. Kluczowa decyzja: wzbogacenie metadanymi z Object Managera

Interfejs mobilny to definicja UI - spłaszcza obiekty do widgetów i gubi typ.
Stąd dzisiejsze problemy:

- LED RGBW wystawia tylko HSB, brak białego kanału (musieliśmy syntetyzować index 12/15),
- DIN przychodzi jako generyczny `sensor` 0/1 zamiast `binary_sensor`/`event`,
- brak `device_class`, jednostki i `state_class` dla wejść analogowych,
- RGB i RGBW nierozróżnialne.

Rozwiązanie: oprócz interfejsu mobilnego odpytać Object Managera o **typ i cechy
każdego obiektu** i z tego wyprowadzać encję. Mapowanie wartości (indeksy cech)
przestaje być zgadywaniem.

Indeksy cech obiektu LED RGBW (potwierdzone w tej instalacji, do skatalogowania):
Value=0, Hue=1, Saturation=2, R=3, G=4, B=5, RGBhex=6, White set=12, White read=15.

## 4. Mapowanie typów Grenton -> encje HA

| Typ obiektu | Encja HA | device_class / tryb | Uwaga |
|---|---|---|---|
| DIN (wejście) | `event` lub `binary_sensor` | - | dziś gubione jako numeryczny sensor |
| DOUT (przekaźnik) | `switch` lub `light` | - | typ docelowy z override |
| DIMM (ściemniacz) | `light` | `ColorMode.BRIGHTNESS` | on/off przez SetValue, nie metodę on/off |
| LED RGB | `light` | `ColorMode.HS` | |
| LED RGBW | `light` | `ColorMode.RGBW` | biały z metadanych, bez syntezy |
| ROLETA | `cover` | pozycja + lamele | |
| ANALOG IN | `sensor` | temperature/illuminance/... + jednostka + state_class | |
| THERMOSTAT | `climate` | | |
| Wartość/licznik | `sensor` / `number` | | |

## 5. Typowany model komend

Eliminuje błąd serializacji, który psuł wieloargumentowe wywołania
(`execute(5,"0,1000")` zamiast `execute(5, 0, 1000)`).

```python
@dataclass(frozen=True)
class GrentonCommand:
    clu_id: str
    object_name: str
    call: Literal["method", "attribute", "variable", "script"]
    index: int
    args: tuple[GrentonArg, ...] = ()

    def to_lua(self) -> str:
        rendered = ", ".join(arg.render() for arg in self.args)
        if self.call == "method":
            return f'{self.object_name}:execute({self.index}, {rendered})'
        if self.call == "attribute":
            return f'{self.object_name}:set({self.index}, {rendered})'
        ...
```

`GrentonArg` koduje typ (liczba bez cudzysłowów, string w cudzysłowach,
lista argumentów). Brak ręcznego sklejania w warstwie encji.

## 6. Deklaratywne EntityDescription

```python
@dataclass(frozen=True)
class GrentonEntityDescription(EntityDescription):
    object_type: GrentonObjectType
    platform: Platform
    color_mode: ColorMode | None = None
    device_class: str | None = None
    unit_of_measurement: str | None = None
    state_class: str | None = None
    value_index: int | None = None

DESCRIPTIONS: dict[GrentonObjectType, GrentonEntityDescription] = {
    GrentonObjectType.LED_RGBW: GrentonEntityDescription(
        key="led_rgbw", platform=Platform.LIGHT, color_mode=ColorMode.RGBW,
    ),
    GrentonObjectType.DIN: GrentonEntityDescription(
        key="din", platform=Platform.EVENT,
    ),
    ...
}
```

Logika typu jednego miejsca, nie rozsiana po mapperach.

## 7. Przykład: encja LED RGBW

```python
class GrentonRgbwLight(GrentonBaseEntity, LightEntity):
    _attr_supported_color_modes = {ColorMode.RGBW}
    _attr_color_mode = ColorMode.RGBW

    @property
    def rgbw_color(self) -> tuple[int, int, int, int] | None:
        r, g, b, w = (self._feature_value(i) for i in (3, 4, 5, 15))
        ...

    async def async_turn_on(self, **kwargs):
        if ATTR_RGBW_COLOR in kwargs:
            r, g, b, w = kwargs[ATTR_RGBW_COLOR]
            await self._send(method(index=3, args=[r]))
            await self._send(method(index=4, args=[g]))
            await self._send(method(index=5, args=[b]))
            await self._send(method(index=12, args=[w]))
```

Biały kanał jest natywną częścią encji, bo typ obiektu mówi, że to RGBW.

## 8. Push + kolejka per-CLU

- Subskrypcje (jak natywna): rejestracja kluczy w chunkach <= 30 na socket,
  routing raportu po sockecie, który go zarejestrował.
- Wysyłka serializowana per CLU (asyncio.Lock) - zapobiega gubieniu datagramów
  UDP przy seriach komend (np. scena gasząca 10 świateł naraz).
- Keepalive/ping, reconnect, oznaczanie encji `unavailable` przy utracie sesji.

## 9. Higiena Home Assistant

- `manifest.json`: `iot_class: local_push`, `integration_type: hub`,
  `quality_scale`, `loggers`, stabilne `requirements`.
- `config_entry.runtime_data` zamiast `hass.data` ad hoc.
- Stabilne `unique_id` (per obiekt/cecha), `DeviceInfo` z grupowaniem per moduł.
- `diagnostics.py` ze zredagowanym zrzutem (bez kluczy/PIN).
- Reconfigure: ponowne pobranie interfejsu + sprzątanie osieroconych encji/urządzeń.
- Opcje per-encja: override typu/device_class/jednostki dla przypadków
  niewykrywalnych (RGB vs RGBW, lux vs %).

## 10. Testy

- `pytest-homeassistant-custom-component`.
- Warstwa `domain` i `transport` testowalna bez HA (czysty Python).
- Fixtures z realnymi payloadami interfejsu i raportów subskrypcji.
- Testy: kodowanie komend (w tym wieloargumentowe), parsowanie wartości,
  mapowanie typ-obiektu na encję, logika on/off ściemniacza, RGBW.
- `hassfest` + walidacja HACS w CI.

## 11. Czego unikać (lekcje z obu integracji)

- Sklejanie komend stringami - psuje argumenty wieloelementowe.
- Wyprowadzanie semantyki z widgetu UI - gubi typ obiektu.
- Magiczne indeksy rozsiane po platformach - trzymać w jednym katalogu typów.
- on/off ściemniacza przez dedykowaną metodę - dla kanałów PWM działa tylko SetValue.
- Brak serializacji wysyłki - serie komend gubią datagramy UDP.
- Generyczne sensory bez device_class/jednostki/state_class - brak statystyk i semantyki.
- Brak testów - regresje w protokole wykrywane dopiero na żywym systemie.

## 12. Plan wdrożenia (fazy)

1. Transport + sesja + szyfrowanie + ping (port z natywnej, dodać testy).
2. Discovery: interfejs mobilny + metadane typów z Object Managera -> katalog.
3. Model domenowy: obiekty, cechy, typowane komendy (+ testy).
4. Koordynator: subskrypcje, kolejka per-CLU, stan, unavailable.
5. EntityDescription + platformy (light/switch/sensor/binary_sensor/cover/climate/event).
6. Config flow: setup, reconfigure, opcje per-encja.
7. Diagnostics, manifest, quality_scale, CI (hassfest/HACS), pełne testy.
8. Migracja: stabilne unique_id zgodne z dotychczasowymi, żeby zachować entity_id.
