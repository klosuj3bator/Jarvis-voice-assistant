// hud.qml — scena HUD-a Jarvisa (Qt Quick).
//
// Ten plik opisuje, CO jest na ekranie i JAK się rusza. Rysuje karta
// graficzna, a Python tylko mówi "zmień stan na słucham" (gui.py).
//
// Warstwy od spodu:
//   1. tło           — obraz narysowany raz przez gui.py (siatka, rysunek techniczny)
//   2. pierścienie   — shader hud.frag: podziałka, klamry, segmenty, łuki, fale
//   3. napis J.A.R.V.I.S. i etykieta stanu
//   4. odczyty w rogach: zegar, procesor, pamięć
//
//
// DLACZEGO TO JEST PŁYNNE
// ======================
//
// Klatki odmierza i rysuje Qt (C++), w wątku GUI. Najważniejsza zasada:
// w tym wątku NIE wykonuje się Python — ani przy klatce, ani cyklicznie.
// Nawet zmiana stanu i odczyt procesora przychodzą tu jako sygnały,
// które odbiera JavaScript (blok Connections poniżej).
//
// Dlaczego to takie ważne: Python ma tzw. GIL — kolejkę do interpretera,
// którą potrafi zająć np. Whisper. Gdyby wątek GUI co jakiś czas wołał
// Pythona, musiałby w niej stać, a klatka by się spóźniła. Pomiar:
// jedno wołanie Pythona na sekundę dawało przycięcia do 0,1 s.
//
// Cały ruch pierścieni zależy od JEDNEJ liczby: zegara "czas" (sekundy od
// zmiany stanu). Przesuwa go UniformAnimator — animacja wbudowana w Qt,
// bez ani jednej linijki JavaScriptu na klatkę — a shader sam wylicza
// z niego kąty, oddech, fale i migotanie.

import QtQuick
import QtQuick.Window

Item {
    id: hud

    // ===== Wejście z Pythona =====
    property var stany: ({})          // wszystkie stany — STANY z gui.py
    property QtObject most: null      // obiekt z sygnałami stan() i statystyki()
    property int czasBlyskuBleduMs: 900
    property string rodzina: "Bahnschrift"
    property string rodzinaMono: "Consolas"

    property string stan: "idle"
    readonly property var parametry: stany[stan] || ({})
    property real cpu: 0
    property real ram: 0

    // Odbiór sygnałów z gui.py. To zwykły JavaScript w wątku GUI —
    // nie czeka na Pythona, więc nie przycina animacji.
    Connections {
        target: hud.most
        function onStan(nazwa) {
            hud.stan = nazwa
            // Błąd jest chwilowy: po chwili sam wraca do idle.
            if (nazwa === "error")
                powrotZBledu.restart()
        }
        function onStatystyki(procesor, pamiec) {
            hud.cpu = procesor
            hud.ram = pamiec
        }
    }

    Timer {
        id: powrotZBledu
        interval: hud.czasBlyskuBleduMs
        // Tylko jeśli w międzyczasie stan się nie zmienił.
        onTriggered: if (hud.stan === "error") hud.stan = "idle"
    }

    // ===== Geometria =====
    // Wszystko liczymy od R, więc HUD wygląda tak samo w oknie i na pełnym ekranie.
    readonly property real dpr: Screen.devicePixelRatio
    readonly property real cx: width / 2
    readonly property real cy: height / 2
    readonly property real promien: Math.min(width, height) * 0.37
    readonly property real margines: Math.max(16, Math.min(width, height) * 0.035)
    readonly property real rozmiar: Math.max(10, Math.min(width, height) * 0.017)

    // Barwa tekstów w rogach — przechodzi płynnie przy zmianie stanu.
    property color kolorTekstu: Qt.rgba(58 / 255, 168 / 255, 222 / 255, 1)
    property color akcentTekstu: Qt.rgba(1, 186 / 255, 64 / 255, 1)
    Behavior on kolorTekstu { ColorAnimation { duration: 900; easing.type: Easing.OutCubic } }
    Behavior on akcentTekstu { ColorAnimation { duration: 900; easing.type: Easing.OutCubic } }

    function kolorZ(c, alfa) { return Qt.rgba(c.r, c.g, c.b, alfa) }
    function wektor(rgb) { return Qt.vector3d(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255) }

    // ===== Zmiana stanu =====
    //
    // Jedyne miejsce, w którym JavaScript dotyka animacji pierścieni.
    // Liczymy, gdzie wszystko JEST w tej chwili, i od tego miejsca
    // ruszamy z nowymi prędkościami — bez tego pierścienie skakałyby
    // przy każdej zmianie stanu.
    property real startStanuMs: Date.now()
    property real startPrzejsciaMs: Date.now()

    onParametryChanged: zastosujStan()
    // Stan startowy przychodzi z Pythona razem z wczytaniem sceny, a wtedy
    // onParametryChanged się nie odpala — stąd osobne wywołanie na starcie.
    Component.onCompleted: zastosujStan()

    function zastosujStan() {
        var p = parametry
        if (p.tempo === undefined)
            return
        var teraz = Date.now()
        var t = ((teraz - startStanuMs) / 1000) % dlugoscZegaraS
        var e = pierscienie

        // Gdzie jesteśmy: to samo wyliczenie, które robi shader.
        var katy = Qt.vector4d(e.start.x + e.predkosc.x * t,
                               e.start.y + e.predkosc.y * t,
                               e.start.z + e.predkosc.z * t,
                               e.start.w + e.predkosc.w * t)
        var obrot = e.obrotStart + 200 * t

        // Jak daleko było poprzednie przejście barw (krzywa jak w animatorze).
        var u = Math.min(1, (teraz - startPrzejsciaMs) / czasPrzejsciaMs)
        var postep = 1 - Math.pow(1 - u, 3)
        function mieszaj(a, b) { return a.plus(b.minus(a).times(postep)) }
        function mieszaj1(v) { return v.x + (v.y - v.x) * postep }

        e.kolorStary = mieszaj(e.kolorStary, e.kolorNowy)
        e.akcentStary = mieszaj(e.akcentStary, e.akcentNowy)
        e.kolorNowy = wektor(p.kolor)
        e.akcentNowy = wektor(p.akcent)
        e.biegSila = Qt.vector2d(mieszaj1(e.biegSila), p.bieg > 0 ? 1 : 0)
        e.smugaSila = Qt.vector2d(mieszaj1(e.smugaSila), p.smuga ? 1 : 0)

        e.start = Qt.vector4d(katy.x % 360, katy.y % 360, katy.z % 360, katy.w % 1)
        e.predkosc = Qt.vector4d(p.hud, p.seg, p.wewn, p.bieg)
        e.obrotStart = obrot % 360
        e.tempo = p.tempo
        e.mocMin = p.min
        e.mocMax = p.max
        e.migotSila = p.migot
        e.faleCo = p.fale_co

        kolorTekstu = Qt.rgba(p.kolor[0] / 255, p.kolor[1] / 255, p.kolor[2] / 255, 1)
        akcentTekstu = Qt.rgba(p.akcent[0] / 255, p.akcent[1] / 255, p.akcent[2] / 255, 1)

        startStanuMs = teraz
        startPrzejsciaMs = teraz
        zegar.restart()
        przejscie.restart()
        // Oddech napisu restartujemy o chwilę później: jego zakres i tempo
        // to wiązania (bindings) liczone z parametrów, a Qt nie gwarantuje,
        // że przeliczy je przed tą funkcją. callLater = "po bieżących zmianach".
        Qt.callLater(function() {
            oddechNapisu.restart()
            oddechEtykiety.restart()
        })
    }

    // Zegar kręci się w kółko co godzinę. Wszystkie prędkości obrotu
    // pomnożone przez 3600 s dają pełne obroty, więc na przełomie
    // godziny nic nie drgnie.
    readonly property real dlugoscZegaraS: 3600
    readonly property int czasPrzejsciaMs: 900

    // ===== 1. Tło =====
    // Obraz rysuje gui.py raz na każdy rozmiar okna. Rozmiar jest w adresie,
    // więc po zmianie rozmiaru Qt samo poprosi o nowy.
    Image {
        anchors.fill: parent
        source: width > 0 && height > 0
            ? "image://jarvis/tlo/" + Math.round(width * hud.dpr) + "x" + Math.round(height * hud.dpr)
            : ""
        cache: false
    }

    // ===== 2. Pierścienie — shader =====
    // Właściwości poniżej trafiają do hud.frag pod tymi samymi nazwami.
    ShaderEffect {
        id: pierscienie
        objectName: "pierscienie"   // po tej nazwie znajdują go testy w Pythonie
        width: hud.promien * 2.4
        height: width
        x: hud.cx - width / 2
        y: hud.cy - height / 2
        fragmentShader: "hud.frag.qsb"

        property real bok: width * hud.dpr
        property real promien: hud.promien * hud.dpr
        property real czas: 0
        property vector4d start: Qt.vector4d(0, 0, 0, 0)
        property vector4d predkosc: Qt.vector4d(0, 0, 0, 0)
        property real obrotStart: 0
        property real tempo: 1
        property real mocMin: 0.6
        property real mocMax: 0.7
        property real migotSila: 0
        property real faleCo: 0
        property real przejscie: 1
        property vector3d kolorStary: Qt.vector3d(58 / 255, 168 / 255, 222 / 255)
        property vector3d kolorNowy: Qt.vector3d(58 / 255, 168 / 255, 222 / 255)
        property vector3d akcentStary: Qt.vector3d(1, 186 / 255, 64 / 255)
        property vector3d akcentNowy: Qt.vector3d(1, 186 / 255, 64 / 255)
        property vector2d biegSila: Qt.vector2d(0, 0)
        property vector2d smugaSila: Qt.vector2d(0, 0)

        // Zegar całej animacji — płynie w wątku renderującym.
        UniformAnimator {
            id: zegar
            target: pierscienie
            uniform: "czas"
            from: 0
            to: hud.dlugoscZegaraS
            duration: hud.dlugoscZegaraS * 1000
            loops: Animation.Infinite
        }

        // Płynne przejście barw po zmianie stanu: szybko na początku,
        // łagodnie pod koniec.
        UniformAnimator {
            id: przejscie
            target: pierscienie
            uniform: "przejscie"
            from: 0
            to: 1
            duration: hud.czasPrzejsciaMs
            easing.type: Easing.OutCubic
        }
    }

    // ===== 3. Napis i etykieta stanu =====
    //
    // Oddychają w tym samym rytmie co pierścienie: od minimum do maksimum
    // i z powrotem, po krzywej sinusa (Easing.InOutSine). Pół cyklu oddechu
    // trwa pi / tempo sekund — dokładnie jak cos(tempo * czas) w shaderze.
    readonly property real polOddechuMs: parametry.tempo ? Math.PI / parametry.tempo * 1000 : 1000

    Image {
        id: napis
        source: hud.promien > 0 ? "image://jarvis/napis/" + Math.round(hud.promien * hud.dpr) : ""
        width: implicitWidth / hud.dpr
        height: implicitHeight / hud.dpr
        x: hud.cx - width / 2
        y: hud.cy - height / 2 - hud.promien * 0.02
        cache: false

        SequentialAnimation {
            id: oddechNapisu
            loops: Animation.Infinite
            OpacityAnimator {
                target: napis; easing.type: Easing.InOutSine; duration: hud.polOddechuMs
                from: Math.min(1, 0.55 + 0.45 * (hud.parametry.min || 0.6))
                to: Math.min(1, 0.55 + 0.45 * (hud.parametry.max || 0.7))
            }
            OpacityAnimator {
                target: napis; easing.type: Easing.InOutSine; duration: hud.polOddechuMs
                from: Math.min(1, 0.55 + 0.45 * (hud.parametry.max || 0.7))
                to: Math.min(1, 0.55 + 0.45 * (hud.parametry.min || 0.6))
            }
        }
    }

    Text {
        id: etykieta
        width: hud.promien * 2
        x: hud.cx - width / 2
        y: hud.cy + hud.promien * 0.11
        horizontalAlignment: Text.AlignHCenter
        text: hud.parametry.etykieta || ""
        font.family: hud.rodzina
        font.pixelSize: Math.max(10, hud.promien * 0.048)
        font.letterSpacing: hud.promien * 0.018
        color: hud.kolorZ(hud.kolorTekstu, 235 / 255)

        SequentialAnimation {
            id: oddechEtykiety
            loops: Animation.Infinite
            OpacityAnimator {
                target: etykieta; easing.type: Easing.InOutSine; duration: hud.polOddechuMs
                from: Math.max(0.55, hud.parametry.min || 0.6)
                to: Math.max(0.55, hud.parametry.max || 0.7)
            }
            OpacityAnimator {
                target: etykieta; easing.type: Easing.InOutSine; duration: hud.polOddechuMs
                from: Math.max(0.55, hud.parametry.max || 0.7)
                to: Math.max(0.55, hud.parametry.min || 0.6)
            }
        }
    }

    // ===== 4. Odczyty w rogach =====
    readonly property color jasny: kolorZ(kolorTekstu, 220 / 255)
    readonly property color blady: kolorZ(kolorTekstu, 120 / 255)

    FontMetrics { id: metrykaMala; font.family: hud.rodzina; font.pixelSize: hud.rozmiar }
    FontMetrics { id: metrykaDuza; font.family: hud.rodzina; font.pixelSize: hud.rozmiar * 3 }

    property string godzina: ""
    property string dzien: ""
    Timer {
        // Co ćwierć sekundy, żeby sekundy na zegarze nie "przeskakiwały".
        interval: 250
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: {
            var teraz = new Date()
            hud.godzina = Qt.formatTime(teraz, "hh:mm:ss")
            hud.dzien = Qt.formatDate(teraz, "dd.MM.yyyy")
        }
    }

    // Teksty ustawiamy po LINII BAZOWEJ (dolnej krawędzi liter), jak robił
    // to QPainter: y górnej krawędzi = linia bazowa - wysokość liter nad nią.

    // Lewy górny róg: nazwa, zegar, data.
    Text {
        x: hud.margines; y: hud.margines + hud.rozmiar - metrykaMala.ascent
        font.family: hud.rodzina; font.pixelSize: hud.rozmiar; font.letterSpacing: hud.rozmiar * 0.12
        text: "J.A.R.V.I.S.  //  SYSTEM ONLINE"; color: hud.jasny
    }
    Text {
        x: hud.margines; y: hud.margines + hud.rozmiar * 2 + 8 - metrykaMala.ascent
        font.family: hud.rodzina; font.pixelSize: hud.rozmiar; font.letterSpacing: hud.rozmiar * 0.12
        text: "JUST A RATHER VERY INTELLIGENT SYSTEM"; color: hud.blady
    }
    Text {
        x: hud.margines; y: hud.margines + hud.rozmiar * 6 - metrykaDuza.ascent
        font.family: hud.rodzina; font.pixelSize: hud.rozmiar * 3
        text: hud.godzina; color: hud.jasny
    }
    Text {
        x: hud.margines; y: hud.margines + hud.rozmiar * 7 + 8 - metrykaMala.ascent
        font.family: hud.rodzina; font.pixelSize: hud.rozmiar; font.letterSpacing: hud.rozmiar * 0.12
        text: hud.dzien; color: hud.blady
    }

    // Prawy górny róg: procesor i pamięć z paskami.
    Repeater {
        model: 2
        Item {
            id: odczyt
            required property int index
            readonly property string nazwa: index === 0 ? "CPU" : "RAM"
            readonly property real wartosc: index === 0 ? hud.cpu : hud.ram
            readonly property real szer: hud.rozmiar * 12
            x: hud.width - hud.margines - szer
            y: hud.margines + index * hud.rozmiar * 3
            width: szer

            Text {
                y: hud.rozmiar - metrykaMala.ascent
                font.family: hud.rodzina; font.pixelSize: hud.rozmiar; font.letterSpacing: hud.rozmiar * 0.12
                text: odczyt.nazwa; color: hud.jasny
            }
            Text {
                width: odczyt.szer; height: hud.rozmiar * 1.3
                horizontalAlignment: Text.AlignRight; verticalAlignment: Text.AlignVCenter
                font.family: hud.rodzina; font.pixelSize: hud.rozmiar; font.letterSpacing: hud.rozmiar * 0.12
                text: Math.round(odczyt.wartosc) + " %"; color: hud.jasny
            }
            Rectangle {
                y: hud.rozmiar * 1.6
                width: odczyt.szer; height: Math.max(3, hud.rozmiar * 0.35)
                color: hud.kolorZ(hud.kolorTekstu, 40 / 255)
                Rectangle {
                    height: parent.height
                    width: parent.width * Math.min(100, odczyt.wartosc) / 100
                    // Pasek robi się bursztynowy przy dużym obciążeniu.
                    color: hud.kolorZ(odczyt.wartosc > 80 ? hud.akcentTekstu : hud.kolorTekstu, 210 / 255)
                }
            }
        }
    }

    // Lewy dolny róg: tryb. Prawy dolny: podpowiedzi klawiszy.
    Text {
        x: hud.margines; y: hud.height - hud.margines - metrykaMala.ascent
        font.family: hud.rodzina; font.pixelSize: hud.rozmiar; font.letterSpacing: hud.rozmiar * 0.12
        text: "TRYB  //  " + (hud.parametry.etykieta || ""); color: hud.jasny
    }
    Text {
        x: hud.width - hud.margines - width; y: hud.height - hud.margines - metrykaMala.ascent
        font.family: hud.rodzina; font.pixelSize: hud.rozmiar; font.letterSpacing: hud.rozmiar * 0.12
        text: "ESC  SCHOWAJ      F11  OKNO"; color: hud.blady
    }
}
