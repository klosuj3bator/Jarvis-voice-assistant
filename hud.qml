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
    // Wiersze odczytów w rogu ekranu — gotowe napisy składa gui.py.
    property var odczyty: []
    // Ostatnie akcje Jarvisa, od najnowszej: [{czas, tekst}] (akcje.py).
    property var akcje: []
    // Tryb tapety (gui.py): bez podpowiedzi klawiszy i bez odczytu sieci,
    // ruch z TYKACZA zamiast animatorów (opis przy TYKACZ).
    property bool tapeta: false
    property int klatkiTapety: 60
    // Gra albo film na pełnym ekranie — ruch stoi (opis przy WSTRZYMANIE).
    property bool wstrzymane: false

    // ===== Panel boczny (Panel.qml, dane z panel.py) =====
    // Wysuwa się z prawej krawędzi, gdy monitor jest jeden albo "Tryb
    // 2 ekranów" jest wyłączony (inaczej panel ma własny pasek — pasek_boczny.py).
    property QtObject mostPanelu: null
    property bool panelWHud: false
    property bool panelPrzypiety: false       // "Wysuń panel" z menu przy zegarku
    property bool panelAuto: false            // wysunięty sam, na czas rozmowy
    property var elementyPanelu: []
    readonly property bool panelWysuniety: panelWHud && (panelPrzypiety || panelAuto)
    readonly property real szerokoscPanelu: Math.max(300, Math.min(440, width * 0.22))
    readonly property real uchwyt: panelWHud ? rozmiar * 1.5 : 0
    property real wysuniecie: panelWysuniety ? szerokoscPanelu : 0
    Behavior on wysuniecie { NumberAnimation { duration: 350; easing.type: Easing.OutCubic } }

    Connections {
        target: hud.mostPanelu
        function onWHud(tak) { hud.panelWHud = tak }
        function onPrzypiety(tak) { hud.panelPrzypiety = tak }
        function onElementy(e) { hud.elementyPanelu = e }
    }

    // W trakcie rozmowy panel wysuwa się sam — żeby było widać jej zapis —
    // a 20 sekund po jej końcu chowa się z powrotem.
    onStanChanged: {
        if (stan === "listening" || stan === "processing" || stan === "speaking") {
            if (elementyPanelu.indexOf("rozmowa") >= 0) {
                panelAuto = true
                zwijaniePanelu.stop()
            }
        } else if (panelAuto) {
            zwijaniePanelu.restart()
        }
    }
    Timer { id: zwijaniePanelu; interval: 20000; onTriggered: hud.panelAuto = false }

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
        function onOdczyty(wiersze) {
            hud.odczyty = wiersze
        }
        function onAkcje(lista) {
            hud.akcje = lista
        }
        function onTapeta(wlaczona) {
            hud.tapeta = wlaczona
        }
        function onWstrzymaj(tak) {
            hud.wstrzymane = tak
        }
        function onPostep(procent, opis) {
            hud.postepProcent = procent
            hud.postepOpis = opis
        }
    }

    // Postęp startu (rozruch.py) — pokazywany w stanie "uruchamianie".
    property int postepProcent: 0
    property string postepOpis: ""

    // Zasłona: obraz zwykłej tapety, którym HUD zakrywa się tuż przed zejściem
    // z pulpitu (gui.py, _zakryj_tapeta). Pusta = brak zasłony.
    property string zaslona: ""

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
        // W pauzie czas stoi w chwili jej rozpoczęcia — liczymy tak, jakby było "wtedy".
        var teraz = hud.wstrzymane ? hud.pauzaOdMs : Date.now()
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
        if (hud.wstrzymane) {
            // Nowy stan w trakcie gry (np. rozmowa z Jarvisem) widać od razu,
            // ale bez ruchu i bez płynnego przejścia barw.
            e.kolorStary = e.kolorNowy
            e.akcentStary = e.akcentNowy
            e.biegSila = Qt.vector2d(e.biegSila.y, e.biegSila.y)
            e.smugaSila = Qt.vector2d(e.smugaSila.y, e.smugaSila.y)
            e.czas = 0
            e.przejscie = 1
            return
        }
        if (hud.tapeta) {
            klatka()          // dalej ruch prowadzi tykacz
            return
        }
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

    // ===== WSTRZYMANIE =====
    //
    // Gdy gra albo film zajmuje cały ekran, gui.py wysyła `wstrzymaj`.
    // Zatrzymujemy wszystko, co rusza się co klatkę: zegar pierścieni,
    // przejście barw i oddech napisów. Scena rysuje się wtedy tylko przy
    // zmianie treści — zegar i odczyty raz na sekundę — zamiast 240 razy.
    //
    // Pierścienie zamierają dokładnie tam, gdzie były: "czas" zostaje
    // ustawiony na chwilę pauzy. Przy wznowieniu przesuwamy początek stanu
    // o długość przerwy, więc zastosujStan() podejmuje ruch bez skoku.
    property real pauzaOdMs: 0
    onWstrzymaneChanged: wstrzymane ? zatrzymajRuch() : wznowRuch()

    function zatrzymajRuch() {
        pauzaOdMs = Date.now()
        var e = pierscienie
        zegar.stop()
        przejscie.stop()
        oddechNapisu.stop()
        oddechEtykiety.stop()
        e.czas = ((pauzaOdMs - startStanuMs) / 1000) % dlugoscZegaraS
        var u = Math.min(1, (pauzaOdMs - startPrzejsciaMs) / czasPrzejsciaMs)
        e.przejscie = 1 - Math.pow(1 - u, 3)       // ta sama krzywa co w animatorze
    }

    function wznowRuch() {
        var przerwa = Date.now() - pauzaOdMs
        startStanuMs += przerwa
        startPrzejsciaMs += przerwa
        zastosujStan()
    }

    // ===== TYKACZ — ruch tapety =====
    //
    // Animatory rysują klatkę za klatką tak szybko, jak pozwoli ekran. W zwykłym
    // oknie to dobrze: Qt trzyma się odświeżania monitora (240 Hz = 240 klatek).
    // Na pulpicie Windows tego tempa nie pilnuje — zmierzone 29.09: tapeta na
    // monitorze 60 Hz rysowała 248-377 klatek na sekundę, cztery razy za dużo,
    // i to przez cały dzień, także przy grach w oknie.
    //
    // Dlatego na tapecie ruch prowadzi zwykły Timer: klatkiTapety razy na
    // sekundę liczy te same wartości, które animatory liczyłyby same, a scena
    // rysuje się tylko wtedy, gdy coś się zmieniło — czyli właśnie tyle razy.
    Timer {
        id: tykacz
        interval: Math.round(1000 / hud.klatkiTapety)
        repeat: true
        running: hud.tapeta && !hud.wstrzymane
        onTriggered: hud.klatka()
    }

    function klatka() {
        var teraz = Date.now()
        var p = parametry
        var t = ((teraz - startStanuMs) / 1000) % dlugoscZegaraS
        var u = Math.min(1, (teraz - startPrzejsciaMs) / czasPrzejsciaMs)
        pierscienie.czas = t
        pierscienie.przejscie = 1 - Math.pow(1 - u, 3)       // krzywa OutCubic jak w animatorze
        // Oddech napisów: to samo co OpacityAnimator z krzywą InOutSine —
        // od minimum do maksimum i z powrotem, pół cyklu w pi / tempo sekund.
        var faza = (1 - Math.cos((p.tempo || 1) * t)) / 2
        var moc = (p.min || 0.6) + ((p.max || 0.7) - (p.min || 0.6)) * faza
        napis.opacity = Math.min(1, 0.55 + 0.45 * moc)
        etykieta.opacity = Math.max(0.55, moc)
    }

    // Wejście w tapetę i wyjście z niej w trakcie działania: przekazanie
    // ruchu między animatorami a tykaczem, bez skoku (ten sam startStanuMs).
    onTapetaChanged: {
        if (wstrzymane)
            return                        // pauza i tak wszystko trzyma
        if (tapeta) {
            zegar.stop(); przejscie.stop(); oddechNapisu.stop(); oddechEtykiety.stop()
            klatka()
        } else {
            zastosujStan()                // animatory ruszają od bieżącego miejsca
        }
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

    // Pasek postępu startu pod całą tarczą reaktora — tylko w trakcie
    // uruchamiania. Podziałka kończy się na 1.0 promienia, a poświata
    // gaśnie na 1.15, więc 1.12 to już "pod rdzeniem", ale jeszcze nad
    // napisami w dolnych rogach.
    Item {
        visible: hud.stan === "uruchamianie"
        x: hud.cx - width / 2
        y: hud.cy + hud.promien * 1.12
        width: hud.promien * 0.8
        height: hud.promien * 0.14

        Rectangle {
            width: parent.width; height: Math.max(2, hud.promien * 0.008)
            color: hud.kolorZ(hud.kolorTekstu, 0.18)
            Rectangle {
                height: parent.height
                width: parent.width * hud.postepProcent / 100
                color: hud.kolorZ(hud.kolorTekstu, 0.9)
                // Krótka animacja tylko przy zmianie procentu — scena i tak
                // kręci się wtedy pełną parą.
                Behavior on width { NumberAnimation { duration: 400; easing.type: Easing.OutCubic } }
            }
        }
        Text {
            y: hud.promien * 0.035
            width: hud.promien * 1.3
            x: (parent.width - width) / 2
            horizontalAlignment: Text.AlignHCenter
            elide: Text.ElideRight
            text: hud.postepProcent + " %" + (hud.postepOpis ? "   //   " + hud.postepOpis.toUpperCase() : "")
            font.family: hud.rodzina; font.pixelSize: Math.max(9, hud.promien * 0.032)
            font.letterSpacing: hud.promien * 0.008
            color: hud.blady
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
        // W pauzie raz na sekundę — każda zmiana napisu to jedna klatka.
        interval: hud.wstrzymane ? 1000 : 250
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
        text: "J.A.R.V.I.S.  //  " + (hud.stan === "uruchamianie" ? "URUCHAMIANIE" : "SYSTEM ONLINE")
        color: hud.jasny
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

    // Prawy górny róg: procesor, pamięć, karta graficzna i sieć — każde z paskiem.
    // Na tapecie bez sieci (wiersze "tylkoOkno").
    Repeater {
        model: hud.tapeta ? hud.odczyty.filter(function(w) { return !w.tylkoOkno }) : hud.odczyty
        Item {
            id: odczyt
            required property var modelData
            required property int index
            readonly property real szer: hud.rozmiar * 14
            x: hud.width - hud.margines - szer - hud.wysuniecie - hud.uchwyt
            y: hud.margines + index * hud.rozmiar * 3.6
            width: szer

            Text {
                y: hud.rozmiar - metrykaMala.ascent
                font.family: hud.rodzina; font.pixelSize: hud.rozmiar
                font.letterSpacing: hud.rozmiar * 0.12
                text: odczyt.modelData.etykieta; color: hud.jasny
            }
            Text {
                width: odczyt.szer; height: hud.rozmiar * 1.3
                horizontalAlignment: Text.AlignRight; verticalAlignment: Text.AlignVCenter
                font.family: hud.rodzina; font.pixelSize: hud.rozmiar
                font.letterSpacing: hud.rozmiar * 0.12
                text: odczyt.modelData.wartosc; color: hud.jasny
            }
            Rectangle {
                y: hud.rozmiar * 1.6
                width: odczyt.szer; height: Math.max(3, hud.rozmiar * 0.35)
                color: hud.kolorZ(hud.kolorTekstu, 40 / 255)
                Rectangle {
                    height: parent.height
                    width: parent.width * odczyt.modelData.wypelnienie
                    // Pasek robi się bursztynowy przy dużym obciążeniu
                    // albo wysokiej temperaturze.
                    color: hud.kolorZ(odczyt.modelData.alarm ? hud.akcentTekstu
                                                             : hud.kolorTekstu, 210 / 255)
                }
            }
            // Podpis pod paskiem: szczyt prędkości albo limit temperatury.
            Text {
                y: hud.rozmiar * 2.3
                width: odczyt.szer
                horizontalAlignment: Text.AlignRight
                font.family: hud.rodzina; font.pixelSize: hud.rozmiar * 0.8
                text: odczyt.modelData.podpis; color: hud.blady
            }
        }
    }

    // Lewy dolny róg: ostatnie akcje Jarvisa (akcje.py), nad nimi nagłówek,
    // pod nimi tryb. Najnowsza akcja najjaśniejsza, starsze coraz bledsze.
    Column {
        x: hud.margines
        y: hud.height - hud.margines - metrykaMala.ascent - hud.rozmiar * 1.6 - height
        spacing: hud.rozmiar * 0.45
        visible: hud.akcje.length > 0

        Text {
            font.family: hud.rodzina; font.pixelSize: hud.rozmiar * 0.8
            font.letterSpacing: hud.rozmiar * 0.12
            text: "OSTATNIE AKCJE"; color: hud.blady
        }
        Repeater {
            model: hud.akcje
            Row {
                required property var modelData
                required property int index
                spacing: hud.rozmiar * 0.8
                opacity: Math.max(0.35, 1 - index * 0.14)
                Text {
                    font.family: hud.rodzinaMono; font.pixelSize: hud.rozmiar * 0.9
                    text: parent.modelData.czas; color: hud.kolorZ(hud.akcentTekstu, 200 / 255)
                }
                Text {
                    font.family: hud.rodzina; font.pixelSize: hud.rozmiar
                    text: parent.modelData.tekst; color: hud.jasny
                }
            }
        }
    }
    Text {
        x: hud.margines; y: hud.height - hud.margines - metrykaMala.ascent
        font.family: hud.rodzina; font.pixelSize: hud.rozmiar; font.letterSpacing: hud.rozmiar * 0.12
        text: "TRYB  //  " + (hud.parametry.etykieta || ""); color: hud.jasny
    }
    // Prawy dolny róg: podpowiedzi klawiszy (na tapecie klawiatura do HUD-a
    // nie trafia) albo informacja, czemu ruch stoi.
    Text {
        x: hud.width - hud.margines - width - hud.wysuniecie - hud.uchwyt
        y: hud.height - hud.margines - metrykaMala.ascent
        font.family: hud.rodzina; font.pixelSize: hud.rozmiar; font.letterSpacing: hud.rozmiar * 0.12
        text: hud.wstrzymane ? "PAUZA  //  PEŁNY EKRAN"
                             : hud.tapeta ? "TAPETA" : "ESC  SCHOWAJ      F11  OKNO"
        color: hud.wstrzymane ? hud.kolorZ(hud.akcentTekstu, 200 / 255) : hud.blady
    }

    // ===== 5. Panel boczny, wysuwany z prawej =====
    // Uchwyt: wąski pasek przy krawędzi, widoczny także przy schowanym panelu.
    Rectangle {
        visible: hud.panelWHud
        x: hud.width - hud.uchwyt - hud.wysuniecie
        width: hud.uchwyt; height: hud.height
        color: hud.kolorZ(hud.kolorTekstu, 0.07)
        Rectangle { width: 1; height: parent.height; color: hud.kolorZ(hud.kolorTekstu, 0.35) }
        Text {
            anchors.centerIn: parent
            rotation: -90
            text: hud.panelWysuniety ? "PANEL  ▸" : "◂  PANEL"
            font.family: hud.rodzina; font.pixelSize: hud.rozmiar * 0.75
            font.letterSpacing: hud.rozmiar * 0.2
            color: hud.blady
        }
    }
    // Na samej górze: zasłona z tapety (opis przy właściwości "zaslona").
    Image {
        anchors.fill: parent
        z: 1000
        visible: hud.zaslona !== ""
        source: hud.zaslona
        cache: false
    }

    Panel {
        visible: hud.wysuniecie > 0
        x: hud.width - hud.wysuniecie
        width: hud.szerokoscPanelu; height: hud.height
        most: hud.mostPanelu
        elementy: hud.elementyPanelu
        rodzina: hud.rodzina; rodzinaMono: hud.rodzinaMono
        kolor: hud.kolorTekstu; akcent: hud.akcentTekstu
    }
}
