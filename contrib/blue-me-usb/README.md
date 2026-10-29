# Installation Blue&Me USB sur Raspberry Pi Zero W

Le moteur auparavant séparé dans adaptive/ est désormais la version par défaut :
audio_bridge.py reçoit le Bluetooth, engine.py gère le PCM et streamfs fournit
les données MP3 au lecteur USB. Il n'y a plus de migration séparée à lancer.

## Installation

Prérequis : Raspberry Pi Zero W original, Raspberry Pi OS Lite 32 bits
(Raspbian 13), Wi-Fi 2,4 GHz et SSH configurés. Internet est nécessaire pour les
paquets système et les modules Go manquants.

Depuis la racine de votre clone, sur le Pi :

~~~bash
sudo bash contrib/blue-me-usb/install.sh
sudo reboot
~~~

Le script compile uniquement le code Go du clone et utilise son image disque
embarquée. Il ne clone aucun dépôt et n'appelle aucun téléchargement de sources
du projet. Il installe les dépendances, le pont audio, le moteur PCM, BlueALSA,
les quatre services RadioNav et leur cible de démarrage.

Une mise à jour utilise la même commande. Fermer le lecteur et éjecter le disque
USB avant de la relancer : les services sont arrêtés pendant le remplacement.
Les fichiers précédents sont sauvegardés sous
/var/lib/radionav-usb/backups/install-DATE. Ces sauvegardes sont destinées à une
restauration manuelle ; le script ne promet pas de retour automatique.

## Premier jumelage

Après redémarrage, ouvrir une session SSH et garder cet agent interactif ouvert :

~~~bash
sudo bluetoothctl
~~~

À l'invite Bluetooth :

~~~text
power on
agent NoInputNoOutput
default-agent
pairable on
discoverable on
~~~

Le nom du Pi est Giulietta-USB. Si le téléphone ne le voit pas, utiliser une
deuxième session SSH pour appliquer les propriétés explicitement :

~~~bash
sudo busctl set-property org.bluez /org/bluez/hci0 org.bluez.Adapter1 Pairable b true
sudo busctl set-property org.bluez /org/bluez/hci0 org.bluez.Adapter1 DiscoverableTimeout u 0
sudo busctl set-property org.bluez /org/bluez/hci0 org.bluez.Adapter1 Discoverable b true
~~~

Jumeler Giulietta-USB depuis le téléphone. Dans bluetoothctl, afficher les
appareils et approuver l'adresse du téléphone :

~~~text
devices Paired
trust AA:BB:CC:DD:EE:FF
discoverable off
pairable off
quit
~~~

Remplacer l'adresse d'exemple par celle du téléphone. L'enregistrement est
conservé ; le pont tente ensuite de reconnecter le premier appareil approuvé.

## Lecture et vérification

Brancher le port micro-USB marqué USB du Pi avec un câble de données.
Sélectionner la piste 001.mp3 sur le PC ou Blue&Me, puis envoyer la musique
du téléphone vers Giulietta-USB.

~~~bash
systemctl is-active radionav-bluetooth radionav-streamfs radionav-audio radionav-gadget
cat /run/radionav-audio/engine-status.json
journalctl -u radionav-audio -u radionav-streamfs -u radionav-gadget -n 60 --no-pager
~~~

Les métriques MP3 sont disponibles en local sur http://127.0.0.1:5001/stats.
Le journal d'installation est /var/log/radionav-usb/install.log.

## Structure

| Fichier | Rôle |
| --- | --- |
| install.sh | Compile le clone local et installe la version courante |
| audio_bridge.py | Bluetooth, statut AVRCP, capture PCM et encodage LAME |
| engine.py | Tampon PCM, transitions de lecture et adaptation à la gigue |
| test_engine.py | 17 tests du moteur PCM |
| bluetooth_ready.py | Prépare le contrôleur pour les appareils déjà jumelés |
| wait_ready.py | Attend le montage FUSE et envoie le MP3 de démarrage |
| radionav-*.service | Bluetooth, serveur FUSE, audio et présentation USB |
| radionav-usb.target | Démarrage et arrêt coordonnés des services |

fetch_upstream.py, prepare_upstream.py, optimize_scan.py et
finalize-autostart.py sont des outils historiques conservés pour référence.
Ils ne font pas partie du parcours d'installation courant.

## Tests

Depuis la racine du dépôt :

~~~bash
python3 -B -m unittest discover -s contrib/blue-me-usb -p test_engine.py -v
go test -p 1 -count=1 -v ./stream
~~~

LAME doit être disponible pour exercer le test Go d'encodage/décodage réel.

## Validation et limites

La version du moteur intégrée ici est celle utilisée sur le Pi et confirmée
en lecture continue par l'utilisateur après correction des coupures.
La nouvelle organisation du dépôt et son installation complète sur une carte
fraîche restent à vérifier sur le matériel.

Le flux est réencodé en MP3 128 kbit/s, 44,1 kHz stéréo, avec --nores.
Le lecteur peut conserver plusieurs secondes en cache : une latence d'environ
huit secondes a été observée avec VLC sur les pauses et reprises.
Le moteur ne purge pas ce cache et ne transmet pas de commandes CAN.
La piste virtuelle a une taille finie ; les lectures hors séquence restent
une limite du mécanisme. Les titres dynamiques sur RadioNav ne sont pas ajoutés.

La première version avait fonctionné dans la voiture. Une validation PC de
la version courante ne remplace pas un nouvel essai Blue&Me.
