// Panel.qml — panel boczny Jarvisa (dane zbiera panel.py).
//
// Jeden komponent, dwa miejsca:
//   - pasek przy krawędzi drugiego monitora ("Tryb 2 ekranów", pasek_boczny.py),
//   - panel wysuwany z boku HUD-a / tapety (hud.qml), gdy monitor jest jeden.
//
// Elementy od góry: rozmowa na żywo, dzień (pogoda, kalendarz, przypomnienia),
// ważne maile, Spotify, notatki z dziś. Każdy włącza się osobno w Ustawieniach.
//
// Dwie zasady, których tu pilnujemy:
//   1. ŻADNEGO RUCHU CO KLATKĘ. Panel rysuje się tylko wtedy, gdy przyjdą
//      nowe dane (i raz na sekundę przy grającej muzyce — pasek postępu).
//      Tapeta ma wolną kartę graficzną, a pasek nie przeszkadza w grze.
//   2. CUDZE TEKSTY TO ZWYKŁY TEKST. Tematy maili, tytuły wydarzeń czy piosenek
//      mogą zawierać cokolwiek — Text.PlainText pokazuje je dosłownie, więc
//      temat w rodzaju <img src="..."> nie wczyta niczego z internetu.

import QtQuick

Item {
    id: panel

    // ===== Wejście =====
    property QtObject most: null          // gui.MostPanelu — sygnały dane() i elementy()
    property var dane: ({})
    property var elementy: []
    property string rodzina: "Bahnschrift"
    property string rodzinaMono: "Consolas"
    property color kolor: Qt.rgba(58 / 255, 168 / 255, 222 / 255, 1)
    property color akcent: Qt.rgba(1, 186 / 255, 64 / 255, 1)
    property bool pasek: false            // true = osobne okno na drugim monitorze

    Connections {
        target: panel.most
        function onDane(d) { panel.dane = d }
        function onElementy(e) { panel.elementy = e }
    }

    // ===== Wymiary i kolory =====
    readonly property real r: Math.max(11, Math.min(17, width * 0.042))   // wielkość tekstu
    readonly property real odstep: r * 1.25
    readonly property color jasny: Qt.rgba(220 / 255, 242 / 255, 1, 0.92)
    readonly property color zwykly: kolorZ(kolor, 0.95)
    readonly property color blady: kolorZ(kolor, 0.55)

    function kolorZ(c, alfa) { return Qt.rgba(c.r, c.g, c.b, alfa) }
    function ma(element) { return elementy.indexOf(element) >= 0 }
    function czas(ms) {
        var s = Math.max(0, Math.floor(ms / 1000))
        return Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2)
    }

    // ===== Tło: ciemne jak HUD, z cienką linią od strony ekranu =====
    Rectangle {
        anchors.fill: parent
        gradient: Gradient {
            GradientStop { position: 0; color: Qt.rgba(6 / 255, 20 / 255, 29 / 255, panel.pasek ? 1 : 0.93) }
            GradientStop { position: 1; color: Qt.rgba(2 / 255, 7 / 255, 11 / 255, panel.pasek ? 1 : 0.93) }
        }
    }
    Rectangle { width: 1; height: parent.height; color: panel.kolorZ(panel.kolor, 0.6) }

    // ===== Klocki, z których składają się sekcje =====
    component Naglowek: Column {
        property string tekst
        width: parent ? parent.width : 0
        spacing: panel.r * 0.35
        Text {
            text: parent.tekst
            font.family: panel.rodzina; font.pixelSize: panel.r * 0.8
            font.letterSpacing: panel.r * 0.14
            color: panel.zwykly
        }
        Rectangle { width: parent.width; height: 1; color: panel.kolorZ(panel.kolor, 0.3) }
    }
    component Opis: Text {             // uwagi w rodzaju "nic teraz nie gra"
        width: parent ? parent.width : 0
        wrapMode: Text.Wrap; textFormat: Text.PlainText
        font.family: panel.rodzina; font.pixelSize: panel.r * 0.85
        color: panel.blady
    }
    component Wiersz: Row {            // godzina + treść, np. wydarzenie
        property string lewy
        property string prawy
        property color kolorLewego: panel.akcent
        width: parent ? parent.width : 0
        spacing: panel.r * 0.7
        Text {
            id: lewa
            text: parent.lewy; textFormat: Text.PlainText
            font.family: panel.rodzinaMono; font.pixelSize: panel.r * 0.9
            color: parent.kolorLewego
        }
        Text {
            width: parent.width - lewa.width - parent.spacing
            text: parent.prawy; textFormat: Text.PlainText
            wrapMode: Text.Wrap; maximumLineCount: 3; elide: Text.ElideRight
            font.family: panel.rodzina; font.pixelSize: panel.r
            color: panel.jasny
        }
    }

    // ===== Treść =====
    Item {
        anchors.fill: parent
        anchors.margins: panel.r * 1.4
        clip: true

        Column {
            id: kolumna
            width: parent.width
            spacing: panel.odstep * 1.4

            Text {
                text: "J.A.R.V.I.S.  //  PANEL"
                font.family: panel.rodzina; font.pixelSize: panel.r * 0.9
                font.letterSpacing: panel.r * 0.16
                color: panel.zwykly
            }

            Opis {
                visible: panel.elementy.length === 0
                text: "Wybierz elementy panelu w Ustawieniach Jarvisa."
            }

            // --- ROZMOWA NA ŻYWO ---
            Column {
                width: parent.width; spacing: panel.odstep * 0.6
                visible: panel.ma("rozmowa")
                Naglowek { tekst: "ROZMOWA" }
                Opis {
                    visible: !(panel.dane.rozmowa && panel.dane.rozmowa.length)
                    text: "Powiedz „Hey Jarvis” — rozmowa pojawi się tutaj."
                }
                Repeater {
                    model: panel.dane.rozmowa || []
                    Column {
                        required property var modelData
                        required property int index
                        width: parent.width; spacing: panel.r * 0.2
                        // Starsze wypowiedzi bledną — oko od razu trafia na ostatnią.
                        opacity: Math.max(0.45, 1 - ((panel.dane.rozmowa.length - 1 - index) * 0.12))
                        Row {
                            spacing: panel.r * 0.6
                            Text {
                                text: modelData.kto === "ty" ? "TY" : "JARVIS"
                                font.family: panel.rodzina; font.pixelSize: panel.r * 0.75
                                font.letterSpacing: panel.r * 0.12
                                color: modelData.kto === "ty" ? panel.akcent : panel.zwykly
                            }
                            Text {
                                text: modelData.czas + (modelData.przerwane ? "   ⏸ przerwane" : "")
                                font.family: panel.rodzinaMono; font.pixelSize: panel.r * 0.75
                                color: panel.blady
                            }
                        }
                        Text {
                            width: parent.width
                            text: modelData.tekst; textFormat: Text.PlainText
                            wrapMode: Text.Wrap; maximumLineCount: 5; elide: Text.ElideRight
                            font.family: panel.rodzina; font.pixelSize: panel.r
                            color: panel.jasny
                        }
                    }
                }
            }

            // --- DZIEŃ: pogoda, kalendarz, przypomnienia ---
            Column {
                id: dzien
                readonly property var d: panel.dane.dzien || ({})
                readonly property var pogoda: d.pogoda || null
                width: parent.width; spacing: panel.odstep * 0.55
                visible: panel.ma("dzien")
                Naglowek { tekst: "DZIŚ" + (dzien.d.data ? "  //  " + dzien.d.data.toUpperCase() : "") }

                Row {
                    visible: !!dzien.pogoda
                    spacing: panel.r * 0.8
                    Text {
                        text: dzien.pogoda ? dzien.pogoda.znaczek : ""
                        font.pixelSize: panel.r * 2.2; color: panel.jasny
                    }
                    Column {
                        anchors.verticalCenter: parent.verticalCenter
                        Text {
                            text: dzien.pogoda ? dzien.pogoda.teraz + "  " + dzien.pogoda.opis : ""
                            textFormat: Text.PlainText
                            font.family: panel.rodzina; font.pixelSize: panel.r * 1.15; color: panel.jasny
                        }
                        Text {
                            text: dzien.pogoda ? dzien.pogoda.zakres + "   opady " + dzien.pogoda.opady
                                                 + "   " + dzien.pogoda.miasto : ""
                            textFormat: Text.PlainText
                            font.family: panel.rodzina; font.pixelSize: panel.r * 0.8; color: panel.blady
                        }
                    }
                }
                Repeater {
                    model: dzien.d.wydarzenia || []
                    Wiersz {
                        required property var modelData
                        lewy: modelData.godzina
                        prawy: modelData.tytul + (modelData.zaproszenie ? "  (zaproszenie)" : "")
                    }
                }
                Repeater {
                    model: dzien.d.przypomnienia || []
                    Wiersz {
                        required property var modelData
                        lewy: "⏰ " + modelData.godzina
                        prawy: modelData.tresc
                        kolorLewego: panel.zwykly
                    }
                }
                Opis {
                    visible: !(dzien.d.wydarzenia && dzien.d.wydarzenia.length)
                             && !(dzien.d.przypomnienia && dzien.d.przypomnienia.length)
                    text: "Na dziś nic więcej w planie."
                }
                Opis { visible: !!dzien.d.uwaga; text: dzien.d.uwaga || "" }
            }

            // --- WAŻNE MAILE ---
            Column {
                id: maile
                readonly property var m: panel.dane.maile || ({})
                width: parent.width; spacing: panel.odstep * 0.55
                visible: panel.ma("maile")
                Naglowek { tekst: "WAŻNE MAILE" }
                Repeater {
                    model: maile.m.lista || []
                    Column {
                        required property var modelData
                        width: parent.width; spacing: panel.r * 0.15
                        Wiersz {
                            lewy: modelData.kiedy
                            prawy: modelData.nadawca + (modelData.konto ? "   ·  " + modelData.konto : "")
                            kolorLewego: panel.blady
                        }
                        Text {
                            width: parent.width
                            text: modelData.temat; textFormat: Text.PlainText
                            wrapMode: Text.Wrap; maximumLineCount: 2; elide: Text.ElideRight
                            font.family: panel.rodzina; font.pixelSize: panel.r * 0.85
                            color: panel.zwykly
                        }
                    }
                }
                Opis { visible: !!maile.m.uwaga; text: maile.m.uwaga || "" }
            }

            // --- SPOTIFY ---
            Column {
                id: spotify
                readonly property var s: panel.dane.spotify || ({})
                // Pasek postępu przesuwamy sami między odczytami (co 3 s) —
                // raz na sekundę, tylko gdy muzyka gra i panel jest widoczny.
                property real teraz: Date.now()
                readonly property real postep: Math.min(s.dlugosc_ms || 0,
                    (s.postep_ms || 0) + (s.gra ? Math.max(0, teraz - (s.odczyt_ms || teraz)) : 0))
                width: parent.width; spacing: panel.odstep * 0.55
                visible: panel.ma("spotify")

                Timer {
                    interval: 1000; repeat: true
                    running: panel.visible && spotify.visible && !!spotify.s.gra
                    onTriggered: spotify.teraz = Date.now()
                }

                Naglowek { tekst: "SPOTIFY" + (spotify.s.tytul && !spotify.s.gra ? "  //  PAUZA" : "") }
                Row {
                    visible: !!spotify.s.tytul
                    width: parent.width; spacing: panel.r * 0.9
                    Image {
                        id: okladka
                        visible: !!spotify.s.okladka
                        width: visible ? panel.r * 5.2 : 0; height: panel.r * 5.2
                        source: spotify.s.okladka || ""
                        fillMode: Image.PreserveAspectCrop
                        asynchronous: true
                    }
                    Column {
                        width: parent.width - (okladka.visible ? okladka.width + parent.spacing : 0)
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: panel.r * 0.25
                        Text {
                            width: parent.width
                            text: spotify.s.tytul || ""; textFormat: Text.PlainText
                            wrapMode: Text.Wrap; maximumLineCount: 2; elide: Text.ElideRight
                            font.family: panel.rodzina; font.pixelSize: panel.r * 1.05; color: panel.jasny
                        }
                        Text {
                            width: parent.width
                            text: spotify.s.wykonawca || ""; textFormat: Text.PlainText
                            elide: Text.ElideRight
                            font.family: panel.rodzina; font.pixelSize: panel.r * 0.85; color: panel.zwykly
                        }
                    }
                }
                Rectangle {
                    visible: !!spotify.s.tytul
                    width: parent.width; height: Math.max(3, panel.r * 0.3)
                    color: panel.kolorZ(panel.kolor, 0.18)
                    Rectangle {
                        height: parent.height
                        width: spotify.s.dlugosc_ms ? parent.width * spotify.postep / spotify.s.dlugosc_ms : 0
                        color: panel.kolorZ(panel.kolor, 0.85)
                    }
                }
                Text {
                    visible: !!spotify.s.tytul
                    width: parent.width; horizontalAlignment: Text.AlignRight
                    text: panel.czas(spotify.postep) + " / " + panel.czas(spotify.s.dlugosc_ms || 0)
                    font.family: panel.rodzinaMono; font.pixelSize: panel.r * 0.75; color: panel.blady
                }
                Opis { visible: !!spotify.s.uwaga; text: spotify.s.uwaga || "" }
            }

            // --- NOTATKI Z DZIŚ ---
            Column {
                width: parent.width; spacing: panel.odstep * 0.55
                visible: panel.ma("notatki")
                Naglowek { tekst: "NOTATKI Z DZIŚ" }
                Repeater {
                    model: panel.dane.notatki || []
                    Wiersz {
                        required property var modelData
                        lewy: modelData.godzina; prawy: modelData.tresc
                    }
                }
                Opis {
                    visible: !(panel.dane.notatki && panel.dane.notatki.length)
                    text: "Dziś jeszcze nic. Powiedz: „zanotuj, że…”"
                }
            }
        }
    }
}
