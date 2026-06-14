<p align="center">
  <img width="678" height="678" alt="LangMux" src="https://github.com/user-attachments/assets/3e54c44a-d52c-4733-b9d0-edb2fa65d660" />
</p>

*__Read this in:__ [English](README.md) | Français*

Réunit une piste **VO japonaise (VOSTFR)** et un **doublage français (VF)** d'un
même épisode en un seul `.mkv` propre et correctement nommé — depuis une
interface web ou en ligne de commande. Pensé pour les animes, mais marche pour
n'importe quelle paire de fichiers de langues différentes.

Pour chaque épisode, le fichier de sortie contient :
- la vidéo,
- l'audio **japonais** (piste par défaut, sauf choix contraire),
- l'audio **français** (VF),
- les **sous-titres français**, *forcés uniquement si l'audio principal n'est pas le français*.

Deux comportements à connaître :
- **Le français synchronisé est privilégié.** Si le fichier VO contient déjà une
  piste audio française (release « MULTI »), c'est elle qui est utilisée — elle
  est forcément synchronisée avec sa propre vidéo — et le fichier VF séparé est
  ignoré. Sinon, l'audio français est pris dans le fichier VF séparé.
- **Vérification de synchro.** Avant chaque fusion, la durée (et le nombre
  d'images/seconde) des deux fichiers sont comparées pour repérer un risque de
  désynchronisation.

---

## Pré-requis

Juste **Docker**. `mkvtoolnix` (mkvmerge) et `ffmpeg` (ffprobe) sont inclus dans
l'image.

## Démarrage rapide

Édite les volumes dans `docker-compose.yml`, puis :

```bash
docker compose up -d --build
```

Ouvre l'interface : **http://IP-DU-SERVEUR:8080**.

> Sur Proxmox, lance ceci dans la VM/LXC où tourne Docker. Tout ce qui est monté
> sous `/media` est la **racine** visible depuis l'interface.

### Exemple de docker-compose.yml

Monte autant de sources que tu veux sous `/media` — chacune apparaît comme un
sous-dossier à la racine de l'interface. Garde les lignes qui correspondent à
l'OS de ton hôte :

```yaml
services:
  langmux:
    build: https://github.com/Arubinu/LangMux.git
    image: langmux:latest
    container_name: langmux
    ports:
      - "8080:8080"
    volumes:
      - ./media:/media/Docker            # dossier local à côté du compose (tout OS)
      - /media:/media/Medias             # Linux : un chemin de l'hôte
      - C:/Users:/media/Utilisateurs     # Windows : un dossier Windows
    environment:
      - LANGMUX_ROOT=/media
    restart: unless-stopped
```

`LANGMUX_ROOT` doit correspondre au point de montage interne (`/media` ici) :
c'est là que démarre le navigateur de l'interface, et il est volontairement
bloqué pour ne pas pouvoir remonter au-dessus.

### Ou en Docker « pur »

```bash
docker build -t langmux .
docker run -d --name langmux -p 8080:8080 -v /chemin/vers/tes/animes:/media langmux
```

Sous Windows (PowerShell), utilise des slashs `/` et des guillemets :

```powershell
docker run -d --name langmux -p 8080:8080 -v "D:/Anime:/media" langmux
```

---

## Utiliser l'interface web

1. **Choisir le dossier** — navigue jusqu'au dossier de la saison (celui qui
   contient les fichiers VF *et* VOSTFR), puis « Analyser ». Les fichiers et
   dossiers cachés ne sont pas affichés.
2. **Série & options** — recherche la série (résultats avec **année** et lien
   **IMDB** pour vérifier ; le **nombre officiel d'épisodes** de la saison
   détectée est affiché pour contrôler que tu les as tous). Choisis la piste
   audio principale, le modèle de nommage, la saison, le sous-dossier de sortie
   et l'écart de durée toléré.
3. **Vérifier le plan** — chaque épisode montre le nom de sortie, les deux
   sources (badges JP/VF), l'écart de durée (alerte si trop grand ou si les fps
   diffèrent) et permet d'inverser VF/VOSTFR si besoin. Les épisodes
   **orphelins** (une seule langue) sont listés mais jamais fusionnés.
4. **Fusion** — une vue visuelle avec un anneau de progression, une tuile par
   épisode qui s'allume une fois faite, des compteurs en direct, et un bouton
   **Annuler** qui arrête proprement (le fichier en cours est supprimé, les
   épisodes restants ne sont pas touchés). Un journal détaillé reste disponible
   en dessous.

Les originaux ne sont jamais modifiés ; les résultats sont écrits dans le
sous-dossier de sortie (`merged/` par défaut).

## En ligne de commande (toujours dispo)

Le même moteur s'utilise en CLI dans le container :

```bash
docker exec -it langmux langmux /media/Nom.De.La.Serie
docker exec -it langmux langmux --dry-run /media/Nom.De.La.Serie
docker exec -it langmux langmux --self-test
```

---

## Notes

- Recherche de séries via **TVMaze** (gratuit, sans clé) ; renvoie le lien IMDB
  pour vérification. IMDB n'ayant pas d'API publique gratuite, c'est la source
  la plus fiable et sans configuration.
- Le suivi des fusions est gardé **en mémoire** : le serveur tourne en un seul
  processus (waitress). Ne pas le passer en multi-worker.
- « Sous-titres forcés » au sens MKV = affichés même quand les sous-titres sont
  désactivés. Sur une piste FR complète en VOSTFR, c'est l'effet recherché.
- Une durée identique ne garantit pas une synchro parfaite (un décalage constant
  reste possible) : c'est une présomption, pas une certitude.
- Si le fichier VO contient déjà une piste française, langmux l'utilise et
  **ignore** le fichier VF séparé. Dis-le si tu préfères toujours forcer le
  fichier séparé.

---

## Licence

[MIT](LICENSE)
