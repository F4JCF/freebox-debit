# Freebox Débit

Programme Windows qui montre **quel appareil consomme la bande passante** sur une Freebox (testé pour la Freebox Delta), et qui **teste le débit Internet** du PC.

## Utilisation

1. Téléchargez `FreeboxDebit.exe` (onglet *Releases* ou *Actions*) et lancez-le. Rien à installer.
2. Première fois : cliquez sur **Demander l'autorisation**, puis répondez **Oui** sur l'écran du Freebox Server.
3. La fenêtre affiche chaque appareil avec son adresse IP, son type de connexion et son débit, actualisés toutes les 2 secondes. L'appareil qui prend toute la bande est surligné en rouge.
4. **Tester le débit** : mesure la latence, la réception et l'envoi (serveurs Cloudflare).

Double-clic sur une ligne : adresse MAC, fabricant, signal Wi-Fi.

## Limites

- **Wi-Fi** : débit mesuré par appareil.
- **Ethernet** : la Freebox ne donne que le débit **par port**. Si un switch est branché sur un port, ses appareils sont regroupés sur une ligne.
- Les débits par appareil incluent le trafic local (ex. PC ↔ NAS), pas seulement Internet.

L'autorisation est enregistrée dans `%APPDATA%\FreeboxDebit\config.json`. Pour la révoquer : Freebox OS → Paramètres → Gestion des accès → Applications.

## Compilation

GitHub Actions construit l'exe à chaque push (`.github/workflows/build.yml`) avec PyInstaller. Python : bibliothèque standard uniquement.
