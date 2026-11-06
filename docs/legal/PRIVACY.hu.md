# PotatoBot Adatkezelési tájékoztató

**Hatálybalépés:** 2026-11-06
**Utolsó frissítés:** 2026-11-06
**Verzió:** 1.1

Ez a dokumentum angol és magyar nyelven készült. Az angol szöveg
(`PRIVACY.md`) az irányadó; a magyar fordítás a könnyebb megértést szolgálja.
Eltérés esetén az angol szöveg az irányadó.

---

## 1. Ki a felelős, és mire vonatkozik ez a tájékoztató

### 1.1 Az adatkezelő

Az 1.2 pontban felsorolt telepítések adatkezelője:

> **Domina Máté**, **Magyarország** területén lakóhellyel rendelkező
> magánszemély
> Adatvédelmi kapcsolat: **privacy@potatobot.win**
> Discord: **@woodenpotato**

A továbbiakban: „mi”, „az üzemeltető”. Adatvédelmi tisztviselőt nem jelöltünk
ki; e hobbiprojekt mérete és jellege ezt nem teszi szükségessé. Az adataiddal
kapcsolatos minden kérdéssel és kéréssel a fenti címen fordulhatsz hozzánk.

### 1.2 A tájékoztató hatálya alá tartozó telepítések

A PotatoBot nyílt forráskódú szoftver. Bárki letöltheti és futtathatja a saját
példányát. **Ez a tájékoztató kizárólag az általunk személyesen üzemeltetett
telepítésekre vonatkozik**, amelyek mindegyike külön Discord-alkalmazás, saját
adatbázissal és saját szerverekkel:

| Discord-alkalmazás | Irányítópult címe | Állapot |
| --- | --- | --- |
| **Potato Bot** (privát alkalmazás) | `https://devboard.potatobot.win` | Saját közösségi szerverünket szolgálja ki, meghívás alapján. Több szervert is kiszolgálhat. |

A telepítések soha nem cserélnek adatot egymással. A PotatoBot más által
futtatott példánya **az ő** szolgáltatásuk: ők az adatkezelői, és nekik kell
saját adatkezelési tájékoztatót közzétenniük. Ha nem vagy biztos benne, melyik
telepítéssel beszélsz, a bot `/version` parancsa megnevezi, és hivatkozik e
tájékoztató és a Felhasználási feltételek aktuális változatára.

### 1.3 Kapcsolat a Discorddal

A PotatoBot a Discordon fut, és a Discord API-jától kap adatokat. **Nem állunk
kapcsolatban a Discord Inc.-kel, az nem támogat és nem hagyott jóvá minket.**
A Discord saját adatvédelmi szabályzata (`https://discord.com/privacy`)
határozza meg, mit tesz a Discord az adataiddal; ez a tájékoztató azt írja
le, mit teszünk mi azokkal az adatokkal, amelyeket a Discord továbbít nekünk.

### 1.4 Szerveradminisztrátorok

A szerver tulajdonosa vagy adminisztrátora, aki a PotatoBotot hozzáadja egy
Discord-szerverhez, eldönti, mely funkciók legyenek bekapcsolva, mely
csatornákat használják, és milyen moderációs küszöbök érvényesek. A 12. pont
írja le, mit jelent ez számukra. A tárolásért és az itt leírt technikai
adatkezelésért az üzemeltető marad az adatkezelő.

---

## 2. Röviden

- Tároljuk a **Discord felhasználói azonosítódat**, és alatta: az
  érmeegyenleget, a tapasztalati pontot, a szintet, a játék- és
  gacha-előzményeket, a leltárat, az időkorlátok és az utolsó aktivitás
  időbélyegeit, a neked adott figyelmeztetéseket, az általad nyitott
  jegyeket és az ideiglenes hangszobád beállításait. A teljes lista a 4.
  pontban található.
- **Nem** tároljuk a felhasználóneved, a megjelenített neved, az e-mail-címed,
  az IP-címed vagy a profilképed. **Nem** tároljuk az üzeneteid szövegét, két
  apró, az 5. pontban leírt kivétellel (szólánc-szavak és a számlálás aktuális
  értéke).
- Az adataidat saját magunk által üzemeltetett szerveren tároljuk, és éjjelente
  **titkosított** formában mentjük a Google Drive-ra, ahol a másolatok 30
  napig maradnak meg.
- Az egyetlen általad beírt szöveg, amely elhagyja a telepítésünket, a
  **zenekeresés** (ezt a YouTube-nak küldjük). Semmi más, amit beírsz, nem jut
  harmadik félhez.
- Soha nem adunk el adatot, nem jelenítünk meg hirdetést, nem használjuk az
  adataidat MI-modellek tanítására, és nem adjuk át adatkereskedőknek.
- A rólad tárolt összes adatot letöltheted a **`/mydata`** paranccsal, és
  törölheted a **`/deletemydata`** paranccsal, bármikor, anélkül, hogy bárkit
  meg kellene kérned.
- A szerveradminisztrátorok automatikus moderációt kapcsolhatnak be
  (szószűrő és figyelmeztetési küszöbök, amelyek némíthatnak, kirúghatnak vagy
  kitilthatnak). A 10. pont írja le a jogaidat ilyen esetben.

---

## 3. Honnan származnak az adataid

| Forrás | Mi | Hogyan |
| --- | --- | --- |
| **A Discord API-ja** (a fő forrás) | A felhasználói azonosítód, a bottal közös szervereid és csatornáid, a szerepköreid, a hangállapotod, a belépési és kilépési eseményeid, a szerver-boostjaid, valamint az üzenetek tartalma azokban a csatornákban, amelyeket a bot olvashat. A felhasználóneved, megjelenített neved, profilképed és a fiókod létrehozásának dátuma látható a bot számára, de – a 4. pontban jelzett kivételekkel – nem tároljuk. | A Discord eseményként küldi ezeket a botnak a gateway-en és a REST API-n keresztül, mert a botot hozzáadták egy olyan szerverhez, amelynek tagja vagy. Ezeket az adatokat nem közvetlenül tőled kaptuk (GDPR 14. cikk). |
| **Te** | A futtatott parancsok, a megnyomott gombok, a bot űrlapjaiba beírt szövegek (játék neve csapatkereséshez, hangszoba neve, zenekeresés), az adattörlés megerősítése. | Közvetlenül, amikor egy funkciót használsz. |
| **A szerver stábja** | A rólad szóló figyelmeztetéshez, kirúgáshoz, kitiltáshoz vagy szintmódosításhoz fűzött indoklás; hogy ki vagy-e véve az inaktivitási jelentésből. | A szerver egy moderátora írja be. |
| **A Discord naplója (audit log)** | Ki rúgott ki vagy tiltott ki egy tagot, és miért. | Kizárólag annak megállapítására olvassuk, hogy kirúgás, kitiltás vagy önkéntes kilépés történt-e, ha ez a bejelentő funkció be van kapcsolva. |

---

## 4. Mit kezelünk, funkciónként

Az alábbi funkciók mindegyikét az adott szerver adminisztrátorai kapcsolhatják
be vagy ki. Ha egy funkció ki van kapcsolva a szervereden, az adott sorban
szereplő adatokat ott nem gyűjtjük. A „telepítésszintű” azt jelenti, hogy az
érték az adott telepítés által kiszolgált összes szerveren közös; minden más
szerverenként tárolódik.

A jogalapok a GDPR 6. cikk (1) bekezdése szerintiek: **(b)** az általad a bot
használatával kért szolgáltatás teljesítése, **(f)** a soronként megnevezett
jogos érdekünk, és **(c)** jogi kötelezettség.

| Funkció | Tárolt adat (mindig a Discord felhasználói azonosítód alatt) | Cél | Jogalap | Megőrzés |
| --- | --- | --- | --- | --- |
| **Fiók és gazdaság** (telepítésszintű) | Érmeegyenleg, tapasztalati pont, szint, blackjack győzelmek/vereségek száma, megvásárolt módosítók (rablási bónusz, rablás elleni védelem, széfvédelem, védett tartalék), testőr lejárata; az utolsó napi jutalom, utolsó munka, utolsó rablás, utolsó sorozatfrissítés és utolsó aktivitás időbélyege; sorozatszámláló; hogy az inaktivitási jelentés említett-e már. | A gazdaság és a játékok működtetése; dupla kifizetések megelőzése. | (b); visszaélés elleni védelem (f) | Törlésig vagy a megőrzési söprésig (7. pont). Az egyenleg és a szint a törlés után anonim azonosító alatt marad meg (8.3 pont). |
| **Aktivitáskövetés** | Egyetlen „utoljára aktív” időbélyeg, naponta legfeljebb egyszer frissül, amikor üzenetet küldesz vagy hangcsatornába lépsz. Tartalom nélkül. | Chat- és hangjutalmak; inaktivitási jelentés; megőrzési söprés. | (b); (f) közösség működtetése | Mint fent; törléskor kiürítjük. |
| **Szintek és szerepkörök** | A tapasztalati pontból származtatott. A szerepkör-változtatások a Discordon történnek, nem tárolódnak. | Szintjutalmak. | (b) | Mint fent. |
| **Kaszinójátékok** | Minden tét: játék, tét összege, kimenetel, jóváírás, mikor tették meg és mikor zárult le; gördülő nyereményszámláló a rendellenes nyereményráta észlelésére. | A tétek biztonságos lezárása újraindításokon át; kihasználások észlelése. | (b); (f) a játék integritása | A tétsorok a törlés után anonim azonosító alatt maradnak (8.3 pont); a számláló törlődik. |
| **Bolt, leltár, gacha, utalványok** | A birtokolt tárgyak és mennyiségük; minden gacha-húzás (banner, ritkaság, jutalom, pity-számlálók); a birtokolt utalványok és beváltásuk ideje; időkorlátos juttatások, például prémium szerepkör vagy bérelt emoji, matrica, hangtábla-hang, beleértve, melyik stábtag adta vagy teljesítette; a beváltott eszközökre vonatkozó teljesítési kérelmek. | A megvásárolt vagy megnyert dolgok átadása; bérletek lejáratása; dupla kifizetések megelőzése. | (b) | Törlésig. A juttatásokat előbb a Discordon vonjuk vissza. |
| **Napi fejtörők (Everydle: LoLdle, Valdle, DbDle, Genshindle)** | Fejtörő-változatonként egy időbélyeg arról, mikor játszottál utoljára. A tippjeidet **nem** tároljuk. | A napi egyszeri szabály érvényesítése és a jutalmak kifizetése. | (b) | Törlésig. |
| **Számláló és szólánc csatornák** | A lánc aktuális értéke és az utoljára játszó tag azonosítója; minden elfogadott szó, felhasználói azonosító nélkül tárolva. Lásd 5. pont. | A játék működtetése és az ismételt szavak elutasítása. | (b) | Az utolsó játékos azonosítója törléskor eltávolításra kerül; a szavak a lánc visszaállításáig. |
| **Csapatkeresés (LFG)** | Az általad létrehozott hirdetések: a beírt játéknév, hány játékos kell, a csatlakozott tagok azonosítói, a létrehozás ideje. | A hirdetés megjelenítése és frissítése. | (b) | A létrehozástól számított nyolc napig, vagy törlésig. |
| **Ideiglenes hangszobák** | A szobádnak adott név, létszámkorlát, zárolt állapot és bitráta; az általad engedélyezett vagy tiltott tagok azonosítói; az aktuális szobád azonosítója. | A szobád visszaállítása úgy, ahogy hagytad. | (b) | Törlésig (ha egy másik tag törli magát, a te listádról is lekerül). |
| **Jegyek (ticketek)** | Hogy nyitottál jegyet (csatorna-azonosító, típus, mikor), és melyik stábtag vette át. **Jegyszöveget az adatbázisban nem tárolunk**; lásd „Jegyátiratok” a 6. pontban. | Tagonként egy nyitott jegy korlátozása; stábmunkafolyamat. | (b); (f) támogatás | Törlésig. |
| **Moderáció: figyelmeztetések** | Minden figyelmeztetés: az azonosítód, a moderátor azonosítója, a figyelmeztetés címkéje, a moderátor által beírt indoklás, a dátum. A címkénkénti számokat a szerver által beállított küszöbökkel vetjük össze. | A szerver moderálása. | (f) a közösség biztonsága; a szerver szabályai | Amíg a stáb el nem távolítja, vagy amíg nem törlöd magad. Az eltávolított figyelmeztetés tartalma a stáb naplójában a törlésig megmarad (6. pont). |
| **Moderáció: szószűrő** | Rólad semmit nem tárolunk a fenti figyelmeztetésen túl, amelynek indoklása a csatornát nevezi meg, soha nem a szót vagy az üzenetedet. | A szerver szabályainak érvényesítése. | (f) | Mint a figyelmeztetéseknél. |
| **Belépés és szabályzat** | Hány másodperc telt el a belépésed és a szabályzat elfogadása között. | Moderációs elemzés a stábnak. | (f) | Törléskor kiürítjük. |
| **Inaktivitási jelentés** | Az utolsó aktivitás időbélyegét használja. A stáb egy beállítással kivehet tagazonosítókat a jelentésből. | A stáb lássa, ki inaktív 14 napja. | (f) közösségkezelés | Mint fent. |
| **Bejelentések** (belépés, kilépés, kirúgás, kitiltás, boost) | Semmi nem tárolódik. A bejegyzés tartalmazza az említésedet, felhasználóneved, megjelenített neved, felhasználói azonosítód, a fiókod korát, kirúgásnál és kitiltásnál pedig a Discord naplójából a moderátort és az indokot. A profilképedet memóriába töltjük le, és csatolmányként a bejegyzéshez fűzzük, vagy opcionális képkártyára rajzoljuk, hogy a megváltoztatása után is látható maradjon. | A közösség üdvözlése és tájékoztatása. | (f) közösség működtetése | Csak Discord-üzenetként, amely felett a szerver rendelkezik. |
| **Meghívónapló** | Semmi nem tárolódik. Egy stábcsatornába kerülő bejegyzés megnevezi az új tagot, a fiókja korát, a meghívót és a meghívókódot. | A stáb lássa, hogyan érkeznek a tagok. | (f) raid elleni védelem, közösségi biztonság | Csak Discord-üzenetként. |
| **Zene** | Semmi nem tárolódik. A lejátszási sor (mit kértek és ki) a memóriában él, amíg a bot játszik. | Az általad kért hang lejátszása. | (b) | Újraindításkor elvész. |
| **Twitch- és YouTube-értesítések** | Tagadat nincs. A streamer- és csatornanevek szerverbeállítások. | Streamek és videók bejelentése. | (b) a szerver részére | Nem értelmezhető. |
| **Játékfrissítési jegyzetek** | Tagadat nincs. | Frissítések bejelentése. | Nem értelmezhető | Nem értelmezhető. |
| **Adatmegosztási preferenciák** | Egy adatkategóriára rögzített leiratkozásod, időbélyeggel. (Egy jövőbeli többszerveres gazdaság alapja; ma semmi nem olvassa.) | A választásod tiszteletben tartása. | (b) | Törlésig. |
| **Törlés és exportálás** | Maga a kérésed nem tárolódik az azonosítód alatt: a törlés naplóbejegyzése csak az anonim helyettesítő azonosítót és a táblánként törölt sorok számát nevezi meg. | A törlés igazolása. | (c) GDPR elszámoltathatóság | Határozatlan ideig, anonimizálva. |

A szerverbeállítások (csatornák, szerepkörök, árak, sablonok, szűrőszavak)
szerverenként tárolódnak, és nem rólad szólnak, kivéve, hogy **minden
beállítási sor rögzíti az azt utoljára módosító adminisztrátor azonosítóját**,
amelyről a 11. pont szól.

---

## 5. Üzenettartalom

A bot a Discordtól az **Üzenettartalom (Message Content)** kiemelt jogosultságot
kéri. Pontosan a következőkre használja, mindent memóriában, az egyes üzenetek
beérkezésekor:

1. **Előtagos parancsok** (a bot előtagjával, alapértelmezés szerint `?`
   kezdődő üzenetek) értelmezése és végrehajtása.
2. **A szószűrő**, ahol a szerver bekapcsolja, normalizálja az üzenetedet, és
   összeveti a szerver listájával. Az üzenet törlődik, ha a szerver így
   állította be. **Sem az üzeneted, sem az egyező szó nem tárolódik**; a
   stábcsatorna azt tudja meg, melyik listaelem egyezett.
3. **Számláló és szólánc csatornák** beolvassák a beírt számot vagy szót. Az
   aktuális érték tárolódik, hogy a játék túlélje az újraindítást, és minden
   elfogadott szó **a felhasználói azonosítód nélkül** tárolódik, hogy ne
   lehessen újra felhasználni. Ez az egyetlen üzenetszöveg, amely az
   adatbázisban megmarad.
4. **Aktivitási jutalmak** csak azt rögzítik, hogy üzenetet küldtél; a
   tartalmat nem olvassák.
5. **Boost-észlelés** csak az üzenet típusát nézi.

Az üzenettartalmat soha nem naplózzuk, soha nem küldjük harmadik félnek, és
soha nem használjuk modell tanítására.

---

## 6. Hová kerülnek az adataid

### 6.1 Discord (címzett és platform)

Minden, amit a bot közzétesz, szerkeszt vagy csatol, a Discord Inc.-hez kerül,
és onnantól a Discord feltételei és a szerver saját beállításai vonatkoznak
rá. Figyelemre méltó:

- **Jegyátiratok.** Amikor a stáb lezár egy jegyet, a bot összeállítja a
  jegycsatorna üzeneteit (időbélyeg, megjelenített név, szöveg, csatolmányok
  hivatkozásai, legfeljebb 50 000 üzenet legfeljebb öt szövegfájlban), és
  közzéteszi a szerver jegynapló-csatornájában, amelyet csak a tulajdonosnak
  szabadna látnia. Az átiratot nem tároljuk; a Discordon él, amíg a szerver
  megőrzi.
- **Moderációs bejegyzések.** Figyelmeztetések, eszkalációk, szűrőriasztások,
  szintmódosítások, kirúgási és kitiltási értesítők, a figyelmeztetési
  előzményeid (`/modlogs`, csak stáb) és a kaszinó-sebességkorlát riasztása a
  szerver által választott csatornákba kerülnek.
- **Közvetlen üzenetek a bottól.** Csak ezekben az esetekben kapsz DM-et:
  szószűrő-értesítés, a kirúgás vagy kitiltás indoka, az adatexportod és a
  törlési igazolásod.
- **Profilképek.** A bejelentésekhez és képkártyákhoz a bot a memóriába tölti
  az aktuális profilképedet, és csatolmányként tölti fel újra. A lemezünkre
  semmi nem íródik.
- **Más tárhelyekről származó képek.** A szerveradminisztrátorok kép-URL-eket
  állíthatnak be (bejelentési képek, szabályzat-bannerek). Ezeket a Discord
  médiaproxyja tölti le, nem a mi szerverünk, és nem a te adataiddal.

### 6.2 Az általunk igénybe vett adatfeldolgozók

| Címzett | Mit kap | Miért | Hol | Garancia |
| --- | --- | --- | --- | --- |
| **Google LLC** (Google Drive) | A teljes adatbázis éjszakai, **titkosított** másolata. A Google nem tudja olvasni: a fájlt feltöltés előtt a szerverünkön az rclone titkosítási rétege titkosítja, és a kulcsok soha nem hagyják el a szerverünket. | Külső helyszíni biztonsági mentés. | Egyesült Államok | Titkosítás a továbbítás előtt; a Google EU–USA adatvédelmi keretrendszer (Data Privacy Framework) szerinti tanúsítása, tartalékként az általános szerződési feltételek (SCC). |
| **Cloudflare, Inc.** | Az irányítópult felé irányuló minden kérés a Cloudflare peremhálózatán és egy Cloudflare Tunnelen keresztül jut a szerverünkre. A Cloudflare látja az irányítópult minden látogatójának **IP-címét, kérésmetaadatait és TLS-kézfogását**, a `potatobot.win` DNS-szolgáltatója, és a kapcsolati címünkre küldött e-maileket az általunk olvasott postafiókba továbbítja (Cloudflare Email Routing), így kezeli mindannak a tartalmát, amit nekünk írsz. A bot Discord felé irányuló forgalmát nem látja. | Az irányítópult HTTPS-en való közzététele az otthoni kapcsolatunk kitétele nélkül. | EU-s peremhelyszínek és Egyesült Államok | A Cloudflare adatfeldolgozási kiegészítése, EU–USA adatvédelmi keretrendszer szerinti tanúsítása és az általános szerződési feltételek. |
| **Google LLC** (YouTube) | Amikor a zenefunkciót használod: **az általad beírt keresőszöveg vagy a beillesztett YouTube-URL**, valamint a szerverünk IP-címe. Discord-adatot nem küldünk. A hangfolyamot a szerverünk tölti le. | A kért zene megtalálása és lejátszása. | Egyesült Államok | A YouTube önálló adatkezelőként jár el saját adatvédelmi szabályzata szerint; a keresésen túl semmit nem küldünk. |
| **Twitch Interactive, Inc.** | Csak a szerveradminisztrátor által beállított streamerneveket, az alkalmazásunk hitelesítő adataival. Tagadat nincs. | Stream-értesítések. | Egyesült Államok | Tagadathoz nem szükséges; nem küldünk ilyet. |
| **Játékkiadók nyilvános végpontjai** (Valve/Steam, Riot Games, HoYoverse, Mojang) | Senkiről semmit; a szerverünk nyilvános frissítési jegyzet-feedeket kérdez le. | Frissítési jegyzet-értesítések. | Változó | Nem értelmezhető. |
| **GitHub, Inc.** | Egy egyszerű kérés a projekt változásnaplójáért, amikor egy irányítópult-felhasználó megnyitja a változásnapló oldalt. Felhasználói adat nincs. | Kiadási jegyzetek megjelenítése. | Egyesült Államok | Nem értelmezhető. |
| **E-mail-szolgáltató (opcionális)** | Ha az üzemeltető bekapcsolja a hibariasztást, a bot hibanaplójának sorait SMTP-szerveren keresztül e-mailben küldi az üzemeltető saját címére. Ezek a sorok tartalmazhatnak **Discord felhasználói és szerverazonosítókat**, üzenetszöveget vagy felhasználónevet soha. **Ez a telepítés: nincs bekapcsolva.** | Értesülés arról, ha a bot elromlik. | Szolgáltatótól függ | Beállítás nélkül kikapcsolva; TLS a továbbítás során. |

Más adatfeldolgozónk nincs. Nincs analitikai szolgáltatás, hibakövető
szolgáltatás, hirdetési hálózat, MI-szolgáltató és frissítési telemetria.

### 6.3 Tárhely

A bot és az irányítópult saját tulajdonú hardveren fut **Magyarország**
területén, dedikált rendszerfiók alatt, root-jogosultság nélküli konténerben,
adminisztrációs célból kizárólag privát hálózaton, SSH-kulccsal elérhetően. Az
adatbázis egy PostgreSQL-kiszolgáló saját konténerben, ugyanezen fiók alatt,
amely kizárólag a bot konténeréből, privát hálózaton érhető el, és amelyet csak
ez a fiók olvashat.

### 6.4 Nemzetközi adattovábbítás

A 6.2 pontban „Egyesült Államok” jelöléssel szereplő címzettek az Európai
Gazdasági Térségen kívül vannak. A továbbítások az Európai Bizottság
**EU–USA adatvédelmi keretrendszerre** (Data Privacy Framework) vonatkozó
megfelelőségi határozatán alapulnak az az alatt tanúsított címzettek esetében,
egyébként pedig az **általános szerződési feltételeken** (a Bizottság
2021/914 határozata). Az adatbázis-mentés esetében az adatot ezen felül a
szerverünk elhagyása előtt titkosítjuk, így a címzett csak titkosított szöveget
birtokol. Ha az adatvédelmi keretrendszert érvénytelenítenék, az általános
szerződési feltételek továbbra is alkalmazandók. A Discord maga amerikai
vállalat; az általunk a Discordtól kapott adat olyan továbbítás, amelyet a
Discord végez a saját feltételei alapján, nem mi.

### 6.5 Ki láthatja még az adataidat

- **A szervered adminisztrátorai** (a tulajdonos, az Adminisztrátor vagy
  Szerver kezelése jogosultsággal rendelkező tagok) bejelentkezhetnek az
  irányítópultra az adott szerverhez. Ott látják a szerver naplóelőzményeit
  (melyik adminisztrátor mit módosított, a felhasználónevekkel a Discordról
  élőben feloldva), a tagokat megnevező beváltási és teljesítési kérelmeket,
  és a korábbi törlések nyilvántartását (csak anonim azonosítókkal). Más
  szerverek adatait nem látják, és nem olvashatnak tömegesen egyenlegeket vagy
  figyelmeztetéseket; azokhoz a bot saját stábparancsain keresztül jutnak a
  Discordon.
- **Az üzemeltető** mindent olvashat az adatbázisban és a naplókban.
- **Senki más.** Harmadik félnek csak a 6.2 pontban felsorolt esetekben, ha a
  jog előírja, vagy ha kifejezetten kéred, adunk át adatot.

---

## 7. Meddig őrizzük meg az adatokat

| Adat | Megőrzés |
| --- | --- |
| Minden, ami a felhasználói azonosítód alatt tárolódik (4. pont) | Amíg nem törlöd (`/deletemydata`), az üzemeltető a kérésedre nem törli, vagy a **megőrzési söprés** el nem távolítja. A söprés naponta fut, ha az üzemeltető megőrzési időablakot állított be (`data_retention_days`; 0 = határozatlan; **ez a telepítés: 365 nap**), és egy tagot csak akkor töröl, ha az utolsó rögzített aktivitása régebbi az időablaknál **és** már egyetlen, a telepítés által kiszolgált szerveren sincs jelen. Aki kilép, és egy éven belül visszatér, megtartja az előrehaladását; azután tiszta lappal indul. |
| Gazdasági sor, lezárt tétek és jutalomigénylési rekordok | Törlés után anonim negatív azonosító alatt maradnak, minden viselkedési oszlop kiürítve (8.3 pont). |
| Csapatkereső hirdetések | Nyolc nap. |
| Irányítópult-hozzáférés változásjelzői | 24 óra. |
| Sorba állított Discord-műveletek (a „kimenő sor”) | Befejezésük után 30 nap. A törlés a rád vonatkozó sorba állított műveletekben az azonosítódat az anonim azonosítóval váltja fel. |
| Stáb-naplóelőzmények (irányítópult-módosítások, figyelmeztetés-eltávolítások) | A naplózási időablak letelte után törlődnek (`audit_retention_days`; 0 = határozatlan; **ez a telepítés: 90 nap**). Amíg megvannak, törléskor az eljáró adminisztrátor azonosítóját anonim azonosító váltja fel, és az eltávolított figyelmeztetés indoklása kiürül. **Az adattörlési bejegyzések kivételek**, és határozatlan ideig megmaradnak: csak az anonim azonosítót nevezik meg, és ezek igazolják, mi törlődött. |
| Működési naplók (9. pont) | A bot naplófájlja 10 × 5 MB-onként rotálódik (kb. 55 MB). A rendszernaplót ezen a gépen 500 MB-os méretkorlát határolja, kb. két hónap; a tagokat megnevező sorok, amelyeket 2026 novemberéig tartalmazott, e korlát szerint járnak le. |
| Külső helyszíni titkosított mentések | 30 nap, utána automatikusan törlődnek. |
| Migráció előtti mentések a szerveren | Minden adatbázis-frissítés előtt készült teljes másolat, a szerveren tárolva visszaállítási céllal. A törlés nem éri el őket. Az éjszakai mentési feladat törli mindegyiket, amint az időablaknál régebbi, miután az aznapi külső másolat ellenőrzése sikerült. **Ez a telepítés: az általuk védett migráció után 90 nappal.** |
| Memóriában tartott adatok (lejátszási sor, meghívó-pillanatkép, időkorlát-térképek, irányítópult-tokenek és sebességkorlát-számlálók) | Újraindításkor elvesznek; az irányítópult-tokenek legfeljebb 12 óra. |

Az adataid törlése nem terjed ki a biztonsági mentésekre. A törlésed után
visszaállított mentés visszahozná a soraidat; eljárásunk szerint bármely
visszaállítás után a naplóelőzményekben rögzített összes törlést újra
lefuttatjuk. A külső másolatok mindenképpen 30 napon belül elévülnek.

---

## 8. A jogaid, és hogyan élhetsz velük

### 8.1 Hozzáférés és adathordozhatóság: `/mydata`

Futtasd a `/mydata` parancsot bármelyik szerveren, ahol a bot jelen van.
Néhány másodpercen belül a bot közvetlen üzenetben küld egy JSON-fájlt, amely
minden sort tartalmaz, amelyet a telepítés a felhasználói azonosítód alatt
őriz, minden szerveren át, beleértve azokat a sorokat is, ahol moderátorként,
átvevőként, juttatóként vagy tiltott tagként szerepelsz. Minden tábla
5000 soros korlátot kap, és jelzi, ha megvágták. A parancsot 15 percenként
egyszer használhatod. A DM-jeidnek nyitva kell lenniük a szervertagok előtt.

Az export a 4. pont minden tábláját lefedi, beleértve azokat a
stáb-naplósorokat is, amelyek adminisztrátorként vagy egy eltávolított
figyelmeztetés alanyaként neveznek meg, valamint a rád vonatkozó, sorba
állított Discord-műveleteket.

### 8.2 Törlés: `/deletemydata`

Futtasd a `/deletemydata` parancsot, olvasd el a figyelmeztetést, és 60
másodpercen belül nyomd meg a megerősítő gombot. A törlés azonnali és
**telepítésszintű**, mert a gazdaságnak nincs szerverenkénti másolata.
Igazolást kapsz arról, mi törlődött és mi maradt meg. Kérheted az üzemeltetőt
is a törlésre, és az üzemeltető az irányítópultról a Discord-fiókod jelenléte
nélkül is elvégezheti.

### 8.3 Mit őriz meg a törlés, és miért

A törlés anonimizálás. Minden személyes adatot törlünk, és három dolgot
tartunk meg egy friss, véletlenszerű, negatív azonosító alatt, amely soha nem
lehet Discord-azonosító:

- **a gazdasági sorodat** (egyenleg, tapasztalati pont, szint,
  blackjack-számlálók, megvásárolt módosítók), minden időbélyeg, időkorlát és
  sorozat kiürítve;
- **a lezárt kaszinótéteket**;
- **a jutalomigénylési rekordokat**.

Ezeket azért őrizzük meg, hogy a telepítés teljes érmekészletéről igazolható
legyen, hogy a törlés nem változtatta meg, és hogy egy visszatérő tagnak ne
lehessen kétszer kifizetni egy egyszeri jutalmat. Semmi nem köti vissza hozzád
az anonim azonosítót. A függőben lévő téteket előbb visszatérítjük az
egyenlegre. Az aktív prémium szerepköröket és bérleteket a rekordjaik törlése
előtt visszavonjuk a Discordon. Ahol más tagok sorai rád mutattak (egy általad
kiadott figyelmeztetés, egy általad átvett jegy, egy hanglista, amelyen
szerepeltél), a mutatót eltávolítjuk vagy az anonim azonosítóval helyettesítjük.

### 8.4 Helyesbítés

A legtöbb általunk őrzött adatot a szoftver állítja elő, és nem lehet „hibás”
egy elírt név értelmében. A figyelmeztetés indoklása egy moderátor
nyilatkozata; kérd a szerver stábját a módosítására vagy eltávolítására, vagy
fordulj hozzánk.

### 8.5 Korlátozás és tiltakozás

Tiltakozhatsz a jogos érdekünkön alapuló adatkezelés ellen (a 4. pontban (f)
jelölésű sorok). A legtöbb esetben a tiltakozás gyakorlati formája a szerver
elhagyása, amely minden új adatkezelést megállít, és a `/deletemydata`
futtatása. Minden másban írj nekünk.

### 8.6 Jogok a Discorddal szemben

A Discord-fiókod, felhasználóneved, profilképed és üzeneteid a Discord
adatkezelése. Ezekkel kapcsolatos jogaidat a Discordnál érvényesítsd.

### 8.7 Panasz

Jogod van panaszt tenni egy felügyeleti hatóságnál, különösen a lakóhelyed
vagy munkahelyed szerinti EU-tagállamban. Az üzemeltető szerinti hatóság:

> **Nemzeti Adatvédelmi és Információszabadság Hatóság (NAIH)**
> 1055 Budapest, Falk Miksa utca 9–11.
> Postacím: 1363 Budapest, Pf. 9.
> Telefon: +36 1 391 1400
> E-mail: ugyfelszolgalat@naih.hu
> Honlap: https://www.naih.hu

Örülnénk, ha előbb nekünk adnál lehetőséget a válaszra, az 1.1 pontban
megadott címen.

---

## 9. Naplók

A bot és az irányítópult működési naplókat ír egy rotálódó fájlba a
titkosított köteten (14. pont). A rendszernaplóba csak az kerül, amit a
folyamat hiba esetén kiír (hibakövetés, veremkiírás, egy automatikus
újraindítás oka), valamint az adatbázis-szerver hibái az értékeik nélkül;
ezek egyike sem nevez meg téged. A naplófájl tartalmazza:

- a bot saját fióknevét és azonosítóját, szerverazonosítókat,
  csatornaazonosítókat, parancsneveket, időzítéseket és hibatípusokat;
- **a Discord felhasználói azonosítódat** kisszámú sorban: amikor belépsz egy
  szerverre, és a bot regisztrálja a fiókodat vagy automatikus szerepköröket ad,
  valamint amikor egy funkció a kérésed kezelése közben hibázik (például egy
  befejezhetetlen zenekeresés vagy egy kézbesíthetetlen export);
- soha nem tartalmazzák a felhasználóneved, az üzenettartalmat, a
  zenekeresési szöveget, az IP-címed vagy az irányítópult-munkamenet adatait. A
  zenekinyerő saját hibáit naplózás előtt puszta hibatípusra csupaszítjuk. A
  hanglejátszón belüli hiba ritka esetben idézheti a lejátszott folyam URL-jét.

Az irányítópult **nem vezet hozzáférési naplót**: csak a lassú kéréseket
(módszer, útvonal, állapot, időtartam) és a kezeletlen hibákat rögzíti. A
látogatók IP-címét néhány percig a memóriában tartjuk a bejelentkezési
kísérletek sebességkorlátozásához, és soha nem írjuk le.

Ha az üzemeltető bekapcsolja a hibariasztó e-maileket (6.2 pont), a hibasorok
e-mailben elhagyják a szervert. Minden más rajta marad.

---

## 10. Automatizált döntések

A szerveradminisztrátorok a következő automatikus műveleteket állíthatják be.
A szoftver anélkül alkalmazza őket, hogy ember vizsgálná az egyedi esetet:

- **Szószűrő**: a szerver listájával egyező üzenet törölhető, és
  figyelmeztetés rögzíthető.
- **Figyelmeztetési küszöbök**: amikor egy címkét viselő figyelmeztetések száma
  eléri a szerver által beállított küszöböt, a bot némíthatja, kirúghatja vagy
  kitilthatja a tagot. Minden gyárilag szállított küszöb nulla (kikapcsolva);
  a szervernek kell bekapcsolnia.
- **Kaszinó-sebességkorlát**: a rendellenes nyeremény riasztást küld a
  stábnak; tőled automatikusan semmit nem vonunk el.
- **Inaktivitási jelentés**: 14 nap rögzített üzenet vagy hangcsatlakozás
  nélkül egy téged megnevező stábbejegyzést eredményez; ellened nem történik
  intézkedés.
- **Megőrzési söprés** (7. pont).

Ha egy ilyen döntés érint, jogod van emberi beavatkozást kérni, az
álláspontodat kifejteni és a döntést vitatni: fordulj a szerver stábjához, aki
eltávolíthatja a figyelmeztetést és visszavonhatja a némítást, kirúgást vagy
kitiltást, vagy fordulj hozzánk. A küszöbök a szerver szabályai, és a szerver
adminisztrátorai felelnek értük.

---

## 11. Az irányítópult

Az irányítópult a szerveradminisztrátoroknak és az üzemeltetőnek szól. Tagok
nem jelentkeznek be. Ha adminisztrátor vagy:

- **A bejelentkezés** Discord OAuth2-vel történik, kizárólag az `identify` és
  `guilds` hatókörökkel. Soha nem kérjük az e-mail-címed. A bejelentkező oldal
  hivatkozik erre a tájékoztatóra és a Felhasználási feltételekre, és a
  Discord ugyanezeket a hivatkozásokat mutatja az engedélyezési képernyőjén.
- **Amit rólad tárolunk**: a munkamenet alatt egy aláírt **süti**, amely a
  Discord felhasználói azonosítódat, felhasználóneved és profilkép-hash-edet,
  az általad kezelhető szerverek azonosítóit, egy CSRF-tokent és
  munkamenet-időbélyegeket tartalmaz. A süti `HttpOnly`, `Secure`,
  `SameSite=Lax`, 10 perc tétlenség után és legfeljebb 12 óra után lejár. Az
  OAuth hozzáférési és frissítő tokenjeidet **kizárólag a szerver memóriájában**
  tartjuk, soha nem lemezen, és kijelentkezéskor vagy lejáratkor eldobjuk. A
  bejelentkezésedről semmi nem íródik az adatbázisba.
- **Az általad végzett módosítások** az adott szerver naplóelőzményeibe
  kerülnek a felhasználói azonosítóddal, a módosított beállítással, annak régi
  és új értékével és az idővel. Ugyanazon szerver más adminisztrátorai láthatják
  őket, a felhasználóneveddel megjelenítéskor a Discordról feloldva. Ezeket a
  rekordok a 7. pont szerinti naplózási időablak után törlődnek (ezen a
  telepítésen 90 nap); amíg megvannak, törléskor a bennük szereplő
  azonosítódat anonim azonosító váltja fel.
- **Böngészőtárolás**: az irányítópult `localStorage`-t használ a megjelenési
  módodhoz, a megjelenítési nyelvedhez és ahhoz, mely oldalsáv-szakaszokat
  hagytad nyitva. Nem állít be analitikai vagy követő sütit, és nem tölt be
  harmadik féltől származó szkriptet vagy betűtípust. A böngésződ a
  profilképedet és a szerverikonokat közvetlenül a Discord CDN-jéről tölti le.
- **A Cloudflare** az irányítópult előtt áll (6.2 pont).

---

## 12. Ha PotatoBotot használó szervert adminisztrálsz

A bot hozzáadásával és a funkciók bekapcsolásával te döntöd el, mit gyűjtünk a
szervereden. Különösen te döntesz arról, fut-e a szószűrő, a figyelmeztetési
küszöbök, a meghívónapló, a bejelentések, a jegyátiratok és az inaktivitási
jelentés, és hová kerül a kimenetük. Te felelsz azért, hogy:

- csak azt kapcsold be, amire a közösségednek szüksége van, és a tagokat
  megnevező kimeneteket (átiratok, meghívónapló, moderációs napló, `/modlogs`)
  stábra korlátozott csatornákba irányítsd;
- ezt a tájékoztatót megismertesd a tagjaiddal, például a szabályzatodban
  hivatkozva;
- a stábod által a figyelmeztetésekbe írt indoklások és az általad beállított
  képek jogszerűek legyenek;
- az irányítópultot csak az általad kezelt szerverhez használd.

A GDPR szerint az általad hozott konfigurációs döntések tekintetében velünk
együtt adatkezelő lehetsz. Az ebben a tájékoztatóban leírt tárolás és technikai
adatkezelés a miénk marad.

---

## 13. Gyermekek

A Discord a felhasználóitól legalább 13 éves kort követel meg, és idősebbet,
ahol az országuk joga így rendelkezik. A PotatoBot nem irányul senkire, aki az
országa szerinti Discord-korhatár alatt van, és tudatosan nem kezeljük ilyen
személy adatait. Ha úgy véled, hogy e korhatár alatti gyermek adatait őrizzük,
írj nekünk, és töröljük.

---

## 14. Biztonság

- Az adatbázist és a naplókat csak a szolgáltatás saját rendszerfiókja
  olvashatja; a bot root-jogosultság nélküli konténerben fut, minden képesség
  elvetve, csak olvasható kódképpel és rendszerhívás-szűrővel.
- A titkos kulcsok a környezetben élnek, soha nem a kódban, és a nyilvános
  forráskódtárat minden módosításkor átvizsgáljuk kiszivárgott hitelesítő
  adatok után.
- A külső helyszíni mentéseket a szerver elhagyása előtt titkosítjuk.
- Az adminisztráció kizárólag SSH-kulcsos privát hálózaton történik; az
  irányítópult csak HTTPS-en érhető el.
- **Az adataid nyugalmi állapotban titkosítva vannak.** Az adatbázis, az
  egyes frissítések előtt készült másolatok, az éjszakai mentés, amíg
  elkészül, és a bot naplófájlja egy titkosított köteten (LUKS2) vannak,
  amelyet a szerver indulása után kézzel nyitunk meg. Az operációs rendszer
  által kilapozott memória tömörített RAM-ba kerül, soha nem lemezre. 2026
  novembere óta nem kerül téged megnevező naplósor a rendszernaplóba, amely
  ezen a köteten kívül van; a régebbiek a méretkorlátja szerint járnak le
  (7. pont).
- Ha tudomást szerzünk az adataidhoz való jogosulatlan hozzáférésről, 72 órán
  belül értesítjük a felügyeleti hatóságot, ahol a GDPR ezt előírja,
  értesítjük a Discordot a fejlesztői feltételei szerint, és tájékoztatjuk az
  érintett tagokat a bot által kiszolgált szervereken keresztül.

---

## 15. Amit soha nem teszünk

Nem adunk el személyes adatot, nem adjuk bérbe, nem osztjuk meg
adatkereskedőkkel, hirdetési hálózatokkal vagy bármilyen monetizációs
szolgáltatással. A fent leírt játékmechanikán túl nem profilozunk, nem
használunk üzenettartalmat gépi tanulási vagy MI-modellek tanítására, nem
lépünk veled kapcsolatba a Discordon kívül a Discordon keresztül szerzett
adatokkal, és nem küldünk kéretlen közvetlen üzenetet.

---

## 16. A nyílt forráskódú szoftver

A PotatoBot forráskódja a GNU Affero General Public License v3.0 alatt jelenik
meg a `https://github.com/WoodenPotatos/potatobot` címen. A kód nyilvános; az
adataid nem, és soha nem részei a tárolónak. E tájékoztató közzétett példánya
abban a tárolóban, a `docs/legal/` könyvtárban él, így minden módosítása
látható a projekt előzményeiben.

---

## 17. A tájékoztató módosítása

Amikor az adatkezelés változik (új, tároló funkció, új adatfeldolgozó, új
telepítés), frissítjük ezt a tájékoztatót, módosítjuk a hatálybalépés dátumát,
és a változást rögzítjük a projekt változásnaplójában. A lényeges
változásokat a telepítés által kiszolgált szervereken bejelentjük. A magyar
fordítás ugyanabban a módosításban frissül.

---

## 18. Kapcsolat

**Domina Máté** · **privacy@potatobot.win** · Discord:
**@woodenpotato**

Biztonsági sérülékenység esetén írj a kapcsolati címre bizalmasan, és ne tégy
közzé nyilvánosan személyes adatot vagy működő kihasználást. Adatkéréshez a
`/mydata` és a `/deletemydata` gyorsabb az e-mailnél; minden e-mailes kérésre
egy hónapon belül válaszolunk, ahogy a GDPR előírja.
