// hud.frag — pierścienie HUD-a Jarvisa, rysowane przez KARTĘ GRAFICZNĄ.
//
// To jest "shader fragmentów": malutki program, który karta graficzna
// uruchamia OSOBNO DLA KAŻDEGO PIKSELA, wszystkie naraz (tysiące rdzeni
// liczą równolegle). Dostaje współrzędne piksela i ma odpowiedzieć jednym
// pytaniem: jaki kolor ma ten piksel?
//
// Dlatego nie ma tu "narysuj okrąg". Jest odwrotnie: dla danego piksela
// liczymy, jak daleko leży od okręgu, i jeśli blisko — świeci.
//
// Po zmianie tego pliku trzeba go skompilować (Qt wczytuje wersję .qsb):
//     pyside6-qsb --qt6 -o hud.frag.qsb hud.frag

#version 440

layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;

// Parametry z QML. Nazwy MUSZĄ być takie same jak właściwości ShaderEffect
// w hud.qml — Qt łączy je po nazwie.
//
// Najważniejszy jest "czas": sekundy od ostatniej zmiany stanu. To JEDYNA
// liczba, która zmienia się co klatkę — cały ruch (obroty, puls, fale,
// migotanie) shader wylicza z niej sam. Dzięki temu co klatkę trzeba
// przesunąć tylko ten jeden zegar, a robi to wątek renderujący Qt,
// bez udziału Pythona (patrz opis w hud.qml).
layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;
    float qt_Opacity;
    float bok;          // bok kwadratu z HUD-em, w pikselach ekranu
    float promien;      // R — promień podziałki, wszystko inne to jego ułamki
    float czas;         // sekundy od zmiany stanu
    vec4 start;         // kąty w chwili zmiany stanu: podziałka, segmenty, łuki, bieg
    vec4 predkosc;      // ich prędkości (stopnie/s; bieg w obrotach/s)
    float obrotStart;   // gdzie była smuga w chwili zmiany stanu (stopnie)
    float tempo;        // szybkość oddechu
    float mocMin;       // zakres jasności oddechu
    float mocMax;
    float migotSila;    // siła migotania (stan "błąd")
    float faleCo;       // co ile sekund wylatuje fala (0 = wcale)
    float przejscie;    // 0.0 -> 1.0 w ciągu chwili po zmianie stanu
    vec3 kolorStary;    // barwy sprzed zmiany i docelowe — shader płynnie
    vec3 kolorNowy;     // je miesza według "przejscie"
    vec3 akcentStary;
    vec3 akcentNowy;
    vec2 biegSila;      // (stara, nowa): czy światło biegnie po segmentach
    vec2 smugaSila;     // (stara, nowa): czy krąży smuga
};

const float STOPIEN = 3.14159265 / 180.0;
const float PREDKOSC_SMUGI = 200.0;   // stopni na sekundę
const float CZAS_FALI = 1.8;          // ile sekund fala leci od tarczy na zewnątrz

// Wartości wyliczane raz na piksel w main() z "czas" i parametrów stanu.
vec3 kolor;
vec3 akcent;
float moc;

// Pseudolosowa liczba 0.0-1.0 z dowolnej liczby — do migotania.
// Ta sama liczba wejściowa daje zawsze ten sam wynik, więc wszystkie
// piksele w jednej klatce migoczą jednakowo.
float losowa(float n) {
    return fract(sin(n * 12.9898) * 43758.5453);
}

// Wynik składamy warstwami, od spodu, jak na kalkach nakładanych na siebie.
// Przechowujemy go w postaci "premultiplied" (kolor już pomnożony przez
// przezroczystość) — tego oczekuje Qt i tak łatwiej nakładać warstwy.
vec4 wynik = vec4(0.0);

// Kładzie kolor c o przezroczystości a NA dotychczasowy wynik.
void nad(vec3 c, float a) {
    a = clamp(a, 0.0, 1.0);
    wynik = vec4(c * a, a) + wynik * (1.0 - a);
}

// Ile piksel jest "pokryty" kreską o szerokości w, jeśli leży w odległości d
// od jej środka. Brzeg rozmywamy na jednym pikselu — to jest wygładzanie
// krawędzi (antyaliasing). Bez tego okręgi miałyby postrzępione schodki.
float kreska(float d, float w) {
    return clamp(0.5 * w + 0.5 - d, 0.0, 1.0) * min(w, 1.0);
}

// Czy piksel leży w zakresie kątów łuku [start, start + rozp]?
// "okres" to co ile stopni wzór się powtarza: 360 dla pojedynczego łuku,
// 60 dla sześciu klamer, 7.5 dla 48 kresek okręgu przerywanego.
// Na końcach łuku też wygładzamy — dlatego liczymy odległość w pikselach
// (kąt w radianach razy promień), a nie w stopniach.
float wLuku(float kat, float start, float rozp, float r, float okres) {
    float da = mod(kat - start, okres);
    float e = (da <= rozp) ? min(da, rozp - da) : -min(da - rozp, okres - da);
    return clamp(e * STOPIEN * r + 0.5, 0.0, 1.0);
}

// Okrąg z neonową poświatą: szeroka, prawie przezroczysta kreska,
// a na niej wąska i jasna. Tak samo jak w dawnej wersji na procesorze.
void okrag(float r, float rr, vec3 c, float a, float w, bool poswiata) {
    float d = abs(r - rr);
    if (poswiata) nad(c, a * 0.22 * kreska(d, w * 5.0));
    nad(c, a * kreska(d, w));
}

void luk(float r, float kat, float rr, float start, float rozp, float okres,
         vec3 c, float a, float w, bool poswiata) {
    float d = abs(r - rr);
    float k = wLuku(kat, start, rozp, rr, okres);
    if (k <= 0.0) return;
    if (poswiata) nad(c, a * 0.25 * kreska(d, w * 3.2) * k);
    nad(c, a * kreska(d, w) * k);
}

void main() {
    // Współrzędne piksela względem środka (y rośnie w dół, jak na ekranie).
    vec2 p = (qt_TexCoord0 - 0.5) * bok;
    float r = length(p);
    // Kąt jak w Qt: od godziny trzeciej, przeciwnie do ruchu wskazówek zegara.
    float kat = mod(degrees(atan(-p.y, p.x)) + 360.0, 360.0);

    // --- Stan animacji w tej klatce, wyliczony z zegara ---
    float katHud = start.x + predkosc.x * czas;
    float katSeg = start.y + predkosc.y * czas;
    float katWewn = start.z + predkosc.z * czas;
    float bieg = fract(start.w + predkosc.w * czas);
    float glowaSmugi = mod(360.0 - (obrotStart + PREDKOSC_SMUGI * czas), 360.0);

    kolor = mix(kolorStary, kolorNowy, przejscie);
    akcent = mix(akcentStary, akcentNowy, przejscie);
    float biegTeraz = mix(biegSila.x, biegSila.y, przejscie);
    float smugaTeraz = mix(smugaSila.x, smugaSila.y, przejscie);

    // Oddech: jasność faluje między mocMin a mocMax. Zaczyna od minimum —
    // tak samo jak pulsowanie napisu J.A.R.V.I.S. w hud.qml, więc oba
    // oddychają w jednym rytmie.
    float oddech = (1.0 - cos(tempo * czas)) * 0.5;
    // Migotanie: nowa losowa jasność 30 razy na sekundę.
    float migot = 1.0 - losowa(floor(czas * 30.0)) * migotSila;
    moc = (mocMin + (mocMax - mocMin) * oddech) * migot;

    float R = promien;
    float rSeg = R * 0.84;
    float grub = R * 0.062;
    float cienka = max(1.0, R * 0.004);

    // --- 1. Poświata za tarczą i gładkie okręgi ---
    float g = r / (R * 1.15);
    if (g < 1.0) {
        float a = g < 0.6 ? mix(60.0, 22.0, g / 0.6) : mix(22.0, 0.0, (g - 0.6) / 0.4);
        nad(kolor, a / 255.0 * moc);
    }
    okrag(r, R, kolor, 150.0 / 255.0 * moc, max(1.0, R * 0.0035), true);
    okrag(r, rSeg, kolor, 26.0 / 255.0 * moc, grub * 1.9, false);
    okrag(r, rSeg + grub * 0.75, kolor, 120.0 / 255.0 * moc, 1.0, false);
    okrag(r, rSeg - grub * 0.75, kolor, 120.0 / 255.0 * moc, 1.0, false);
    okrag(r, R * 0.76, kolor, 110.0 / 255.0 * moc, 1.0, false);
    okrag(r, R * 0.575, kolor, 170.0 / 255.0 * moc, cienka, true);

    // --- 2. Fale energii: od brzegu tarczy na zewnątrz, blednąc ---
    // Fala numer n wylatuje w chwili n * faleCo (pierwsza od razu po zmianie
    // stanu). Sprawdzamy kilka ostatnich — starsze dawno doleciały.
    if (faleCo > 0.0) {
        float ostatnia = floor(czas / faleCo);
        for (int k = 0; k < 8; k++) {
            float n = ostatnia - float(k);
            if (n < 0.0) break;
            float f = (czas - n * faleCo) / CZAS_FALI;   // 0.0 przy tarczy, 1.0 na zewnątrz
            if (f >= 1.0) break;
            okrag(r, R * (0.56 + 0.62 * f), kolor,
                  170.0 / 255.0 * pow(1.0 - f, 1.6) * moc, cienka, true);
        }
    }

    // --- 3. Podziałka zewnętrzna: 180 kresek co 2 stopnie ---
    // Zamiast rysować 180 kresek, szukamy NAJBLIŻSZEJ kreski dla tego piksela.
    if (r > R * 0.94 - 2.0 && r < R + 2.0) {
        float wzgl = kat - katHud;
        float t = floor(wzgl / 2.0 + 0.5);
        float wBok = abs(wzgl - t * 2.0) * STOPIEN * r;   // odległość w pikselach
        bool duza = int(mod(t, 180.0)) % 5 == 0;          // co 10 stopni dłuższa
        float r1 = R * (duza ? 0.945 : 0.972);
        float wzdluz = clamp(min(r - r1, R - r) + 0.5, 0.0, 1.0);
        nad(kolor, (duza ? 210.0 : 110.0) / 255.0 * moc
                   * kreska(wBok, duza ? cienka : 1.0) * wzdluz);
    }

    // --- 4. Sześć klamer z zaczepami, obracają się w drugą stronę ---
    float klamra = -katHud * 1.6;
    luk(r, kat, R * 0.915, klamra, 16.0, 60.0, kolor, 200.0 / 255.0 * moc, R * 0.016, true);
    luk(r, kat, R * 0.89, klamra - 1.0, 3.0, 60.0, kolor, 170.0 / 255.0 * moc, R * 0.03, false);
    luk(r, kat, R * 0.89, klamra + 14.0, 3.0, 60.0, kolor, 170.0 / 255.0 * moc, R * 0.03, false);

    // Trzy bursztynowe kropki u góry.
    for (int i = 0; i < 3; i++) {
        float a = radians(96.0 - 6.0 * float(i));
        vec2 c = vec2(cos(a), -sin(a)) * R * 0.885;
        float pokr = clamp(R * 0.009 - length(p - c) + 0.5, 0.0, 1.0);
        nad(akcent, 230.0 / 255.0 * moc * (1.0 - 0.2 * float(i)) * pokr);
    }

    // --- 5. Wieniec 72 segmentów z biegnącym światłem i smugą ---
    if (abs(r - rSeg) < grub) {
        float wzgl = mod(kat - katSeg, 360.0);
        float i = floor(wzgl / 5.0);           // numer segmentu
        float lok = wzgl - i * 5.0;            // miejsce w segmencie (0-5 stopni)
        float e = (lok <= 3.2) ? min(lok, 3.2 - lok) : -min(lok - 3.2, 5.0 - lok);
        float pokr = kreska(abs(r - rSeg), grub) * clamp(e * STOPIEN * rSeg + 0.5, 0.0, 1.0);

        if (pokr > 0.0) {
            // Wzór zapalonych segmentów — ten sam co ZAPALONE_ODCINKI w gui.py.
            int n = int(i);
            bool zapalony = n < 27 || (n >= 31 && n < 45) || (n >= 50 && n < 67);
            float jasnosc = zapalony ? 0.78 : 0.156;

            // Biegnące światło: jaśniej, im bliżej "czoła" biegu. Odległość
            // liczymy po okręgu, żeby segmenty 71 i 0 były sąsiadami.
            float odl = abs(bieg - i / 72.0);
            odl = min(odl, 1.0 - odl);
            jasnosc += 0.7 * biegTeraz * pow(max(0.0, 1.0 - odl * 72.0 / 7.0), 2.0);

            // Smuga: jasne, prawie białe czoło z gasnącym ogonem.
            vec3 c = kolor;
            float za = mod(katSeg + i * 5.0 - glowaSmugi, 360.0) / 360.0;
            if (za < 0.2) {
                float sila = (1.0 - za / 0.2) * smugaTeraz;
                jasnosc += 0.9 * sila;
                c = mix(kolor, vec3(1.0), sila * sila);
            }
            nad(c, moc * min(1.0, jasnosc) * pokr);
        }
    }

    // --- 6. Łuki wewnętrzne ---
    luk(r, kat, R * 0.70, 110.0 + katWewn, 64.0, 360.0, akcent, 235.0 / 255.0 * moc, R * 0.020, true);
    luk(r, kat, R * 0.70, 200.0 + katWewn, 18.0, 360.0, akcent, 235.0 / 255.0 * moc, R * 0.020, true);
    luk(r, kat, R * 0.70, 232.0 + katWewn, 5.0, 360.0, akcent, 235.0 / 255.0 * moc, R * 0.020, true);
    // Okrąg przerywany: 48 kresek, kręci się w przeciwną stronę.
    luk(r, kat, R * 0.63, -katWewn * 0.7, 4.0, 7.5, kolor, 150.0 / 255.0 * moc,
        max(1.0, R * 0.006), false);

    fragColor = wynik * qt_Opacity;
}
