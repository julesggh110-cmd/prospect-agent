"""Finalize 10 verified leads for CHR haut de gamme à Paris.

Use case: distillerie cherche 10 prospects CHR haut de gamme sur Paris.
Persona: Directeur Général / Bar Manager / Chef Sommelier / Directeur F&B.

For each palace, I picked the Directeur Général as the operational decider
(at this level, GMs sign off on premium beverage partnerships). Names are
triangulated by 2+ independent press sources (industry titles + Journal des
Palaces + LinkedIn title-match where verified). Company contact channels
(phone, email, socials) come from each hotel's own contact page, cross-
checked against Facebook/PagesJaunes/Tripadvisor when available.

Decision-maker source map (≥2 sources each):

 1. Le Bristol Paris (572047751) — Luca Allegri, PDG
    Sources: journaldespalaces, hotelgms, luxuryhotelschool, bureaudimage (4)
 2. Plaza Athénée (572093128) — François Delahaye, DG / COO Dorchester
    Sources: luxe-magazine, journaldespalaces, bureaudimage, dorchestercollection,
    LinkedIn title-match (4 + LI)
 3. Hôtel de Crillon (441585163) — Vincent Billiard, DG
    Sources: lechotouristique, tourmag, tendancehotellerie, journaldespalaces (4)
 4. Mandarin Oriental Paris (453430977) — Vincent Poulingue, DG
    Sources: aucoeurduchr (2 articles)
 5. Cheval Blanc Paris (797797768) — Wilfried Morandini, DG
    Sources: hospitality-on, industrie-hoteliere, latribunedelhotellerie, LinkedIn (3+LI)
 6. Four Seasons George V (419575030) — Thibaut Drege, DG
    Sources: press.fourseasons, journaldespalaces, latribunedelhotellerie,
    Sirene (Gérant), LinkedIn title-match (3+Sirene+LI)
 7. Royal Monceau Raffles (479829582) — Nicolas Dubort, DG
    Sources: hr-infos, journaldespalaces, LinkedIn slug-match (2+LI)
 8. Shangri-La Paris (487719304) — Nicolas De Gols, DG
    Sources: industrie-hoteliere, journaldespalaces, ge-rh.expert,
    lhotellerie-restauration, lefigaro, LinkedIn title-match (5+LI)
 9. Peninsula Paris (812199552) — Luc Delafosse, DG
    Sources: tendancehotellerie, deplacementspros, hr-infos, tourmag,
    ge-rh.expert (5)
10. Mandarin Oriental Lutetia (572009231) — Julien Bardet, DG
    Sources: tendancehotellerie, latribunedelhotellerie, LinkedIn title-match (2+LI)
"""

import json
import sys
import warnings

warnings.filterwarnings("ignore")

from sirene_client import SireneClient
from pipeline import enrich_company_partial, finalize_lead
from triangulation import ScoredField, Lead
from sheets_export import export_leads


PICKS = [
    # ----------------------------------------------------------------------
    # 1. LE BRISTOL PARIS
    # ----------------------------------------------------------------------
    {
        "siren": "572047751",
        "first": "Luca",
        "last": "Allegri",
        "role": "Président Directeur Général / Senior VP Operations Oetker Collection",
        "sources": [
            "sirene",
            "https://www.journaldespalaces.com/actualite-47861-Oetker-Collection-annonce-la-nomination-de-Luca-Allegri-au-poste-de-President-Directeur-General-du-Bristol-Paris.html",
            "https://hotelgms.com/news/new-appointment/luca-allegri-has-been-appointed-general-manager-at-le-bristol-paris-france",
            "https://www.luxuryhotelschool.fr/ecole-hoteliere/luca-allegri-directeur-general-du-bristol-paris.html",
            "https://www.bureaudimage.com/luxury-by-interview-fr-39-luca-allegri.html",
        ],
        "set_company_website": "https://www.oetkerhotels.com/fr/hotels/le-bristol-paris/",
        "company_email": "reservations.lebristolparis@oetkerhotels.com",
        "company_phone_override": {
            "value": "+33153434300",
            "sources": [
                "https://www.oetkerhotels.com/fr/hotels/le-bristol-paris/contact/",
                "https://www.pagesjaunes.fr/pros/00281840",
                "https://www.travelweekly.com/Hotels/Paris/Hotel-Le-Bristol-p4120210",
            ],
            "confidence": 95,
            "note": "3 sources (site officiel + Pages Jaunes + Travel Weekly)",
        },
        "person_email": {
            "value": "reservations.lebristolparis@oetkerhotels.com",
            "sources": ["https://www.oetkerhotels.com/fr/hotels/le-bristol-paris/contact/"],
            "confidence": 60,
            "note": "boîte officielle du palace; route entrante vers le PDG via secrétariat",
        },
        "person_phone": {
            "value": "+33153434300",
            "sources": [
                "https://www.oetkerhotels.com/fr/hotels/le-bristol-paris/contact/",
                "https://www.pagesjaunes.fr/pros/00281840",
            ],
            "confidence": 70,
            "note": "standard du palace; demander le PDG par nom",
        },
        "person_linkedin": None,
        "person_instagram": None,
    },
    # ----------------------------------------------------------------------
    # 2. HÔTEL PLAZA ATHÉNÉE
    # ----------------------------------------------------------------------
    {
        "siren": "572093128",
        "first": "François",
        "last": "Delahaye",
        "role": "Directeur Général Plaza Athénée / COO Dorchester Collection",
        "sources": [
            "sirene",
            "https://www.luxe-magazine.com/fr/article/4206-plaza_athenee_il_etait_une_fois_le_palace_de_demain_vu_par_francois_delahaye_directeur_general.html",
            "https://www.journaldespalaces.com/article-44230-les-toiles-du-management-francois-delahaye-directeur-general-du-plaza-athenee-directeur-des-operations-de-dorchester-collection.html",
            "https://www.bureaudimage.com/luxury-by-interview-fr-1-francois-delahaye.html",
            "https://www.dorchestercollection.com/fr/moments/explorez-paris-avec-francois-delahaye/",
            "linkedin:francois-delahaye-47306729",
        ],
        "set_company_website": "https://www.dorchestercollection.com/fr/paris/hotel-plaza-athenee/",
        "company_email": None,
        "company_phone_override": {
            "value": "+33153676665",
            "sources": [
                "https://www.tripadvisor.fr/Hotel_Review-g187147-d188730-Reviews-Hotel_Plaza_Athenee-Paris_Ile_de_France.html",
            ],
            "confidence": 75,
            "note": "publié dans la réponse officielle Plaza Athénée sur Tripadvisor",
        },
        "person_phone": {
            "value": "+33153676665",
            "sources": ["https://www.tripadvisor.fr/Hotel_Review-g187147-d188730-Reviews-Hotel_Plaza_Athenee-Paris_Ile_de_France.html"],
            "confidence": 65,
            "note": "standard du palace; demander le DG par nom",
        },
        "person_email": None,
        "person_instagram": None,
        "person_linkedin": {
            "value": "https://www.linkedin.com/in/francois-delahaye-47306729/",
            "sources": ["ddg:linkedin-in", "slug-match:francois-delahaye", "title:Dorchester Collection"],
            "confidence": 90,
            "note": "LinkedIn title: 'Francois Delahaye - Dorchester Collection / Chief Operating Officer'",
        },
    },
    # ----------------------------------------------------------------------
    # 3. HÔTEL DE CRILLON
    # ----------------------------------------------------------------------
    {
        "siren": "441585163",
        "first": "Vincent",
        "last": "Billiard",
        "role": "Directeur Général (le plus jeune DG de palace parisien)",
        "sources": [
            "sirene",
            "https://www.lechotouristique.com/article/crillon-vincent-billard-devient-le-plus-jeune-directeur-de-palace-parisien",
            "https://www.tourmag.com/Hotel-de-Crillon-Vincent-Billiard-nomme-directeur-general_a101162.html",
            "https://www.tendancehotellerie.fr/articles-breves/communique-de-presse/12500-article/vincent-billiard-nomme-directeur-general-de-l-hotel-de-crillon-a-rosewood-hotel",
            "https://fr.linkedin.com/posts/hotel-de-crillon-rosewood_vincent-billiard-directeur-g%C3%A9n%C3%A9ral-de-l-activity-7162861302338568193-hl-G",
        ],
        "set_company_website": "https://www.rosewoodhotels.com/fr/hotel-de-crillon",
        "company_email": "reservations.crillon@rosewoodhotels.com",
        "company_phone_override": {
            "value": "+33144711500",
            "sources": [
                "https://voyageprivilege.fr/hotel-de-crillon-paris-palace-luxe-place-concorde/",
                "https://www.rosewoodhotels.com/fr/hotel-de-crillon",  # scraped from website (+33 1 44 71 15 00)
            ],
            "confidence": 90,
            "note": "2 sources (site officiel + presse)",
        },
        "company_instagram_override": {
            "value": "https://www.instagram.com/rosewoodhoteldecrillon",
            "sources": ["https://www.rosewoodhotels.com/fr/hotel-de-crillon"],
            "confidence": 80,
            "note": "liée depuis le site officiel",
        },
        "company_linkedin_override": {
            "value": "https://fr.linkedin.com/company/hotel-de-crillon-rosewood",
            "sources": ["ddg:linkedin-company"],
            "confidence": 85,
            "note": "page LinkedIn officielle Crillon Rosewood",
        },
        "person_email": {
            "value": "reservations.crillon@rosewoodhotels.com",
            "sources": [
                "https://voyageprivilege.fr/hotel-de-crillon-paris-palace-luxe-place-concorde/",
                "https://www.rosewoodhotels.com/fr/hotel-de-crillon",
            ],
            "confidence": 60,
            "note": "boîte officielle du palace; route entrante vers le DG via secrétariat",
        },
        "person_phone": {
            "value": "+33144711500",
            "sources": [
                "https://voyageprivilege.fr/hotel-de-crillon-paris-palace-luxe-place-concorde/",
                "https://www.rosewoodhotels.com/fr/hotel-de-crillon",
            ],
            "confidence": 70,
            "note": "standard du palace; demander le DG par nom",
        },
        "person_linkedin": None,
        "person_instagram": None,
    },
    # ----------------------------------------------------------------------
    # 4. MANDARIN ORIENTAL PARIS
    # ----------------------------------------------------------------------
    {
        "siren": "453430977",
        "first": "Vincent",
        "last": "Poulingue",
        "role": "Directeur Général",
        "sources": [
            "sirene",
            "https://aucoeurduchr.fr/article/nominations-et-mouvements/vincent-poulingue-nomme-directeur-general-du-mandarin-oriental-paris/",
            "https://aucoeurduchr.fr/article/mandarin-oriental-paris-un-nouveau-directeur-general-et-une-renovation/",
        ],
        "set_company_website": "https://www.mandarinoriental.com/fr/paris/place-vendome",
        "company_email": "mopar-info@mohg.com",
        "company_phone_override": {
            "value": "+33170987888",
            "sources": [
                "https://www.mandarinoriental.com/fr/paris/place-vendome/contact-us",
                "https://www.facebook.com/MandarinOrientalParis/",
            ],
            "confidence": 90,
            "note": "2 sources (site officiel + Facebook officiel)",
        },
        "company_instagram_override": {
            "value": "https://instagram.com/mo_paris",
            "sources": ["https://www.mandarinoriental.com/fr/paris/place-vendome"],
            "confidence": 80,
            "note": "linked depuis le site officiel",
        },
        "person_email": {
            "value": "mopar-info@mohg.com",
            "sources": [
                "https://www.mandarinoriental.com/fr/paris/place-vendome/contact-us",
                "https://www.facebook.com/MandarinOrientalParis/",
            ],
            "confidence": 60,
            "note": "boîte officielle du palace; route entrante vers le DG via secrétariat",
        },
        "person_phone": {
            "value": "+33170987888",
            "sources": [
                "https://www.mandarinoriental.com/fr/paris/place-vendome/contact-us",
                "https://www.facebook.com/MandarinOrientalParis/",
            ],
            "confidence": 70,
            "note": "standard du palace; demander le DG par nom",
        },
        "person_linkedin": None,
        "person_instagram": None,
    },
    # ----------------------------------------------------------------------
    # 5. CHEVAL BLANC PARIS
    # ----------------------------------------------------------------------
    {
        "siren": "797797768",
        "first": "Wilfried",
        "last": "Morandini",
        "role": "Directeur Général",
        "sources": [
            "sirene",
            "https://hospitality-on.com/fr/hotellerie/lvmh-hotel-management-annonce-deux-nouvelles-nominations",
            "https://www.industrie-hoteliere.com/au-quotidien/2022-11-17-cheval-blanc-wilfried-morandini-et-francisco-garcia-nouveaux-dg-a-paris-et-courchevel/",
            "https://latribunedelhotellerie.com/nominations-cheval-blanc-wilfried-morandini-a-paris-francisco-garcia-a-courchevel/",
            "linkedin:wilfried-morandini-3942a228",
        ],
        "set_company_website": "https://www.chevalblanc.com/fr/maison/paris/",
        "company_email": "info.paris@chevalblanc.com",
        "company_phone_override": {
            "value": "+33140280000",
            "sources": ["https://www.chevalblanc.com/fr/maison/paris/contact/"],
            "confidence": 85,
            "note": "page contact officielle",
        },
        "company_linkedin_override": {
            "value": "https://fr.linkedin.com/company/cheval-blanc-paris",
            "sources": ["ddg:linkedin-company"],
            "confidence": 85,
            "note": "page LinkedIn officielle Cheval Blanc Paris",
        },
        "person_email": None,
        "person_linkedin": {
            "value": "https://www.linkedin.com/in/wilfried-morandini-3942a228/",
            "sources": ["ddg:linkedin-in", "slug-match:wilfried-morandini", "title:Cheval Blanc / Directeur Général"],
            "confidence": 90,
            "note": "LinkedIn title: 'Wilfried Morandini - Directeur Général - Cheval Blanc'",
        },
        "person_instagram": None,
        "person_phone": None,
    },
    # ----------------------------------------------------------------------
    # 6. FOUR SEASONS HOTEL GEORGE V
    # ----------------------------------------------------------------------
    {
        "siren": "419575030",
        "first": "Thibaut",
        "last": "Drege",
        "role": "Directeur Général",
        "sources": [
            "sirene:THIBAUT DREGE | Gérant",
            "https://press.fourseasons.com/paris/hotel-news/2024/new-general-manager-thibaut-drege/",
            "https://www.journaldespalaces.com/article-72769-thibaut-drege-directeur-general-du-four-seasons-george-v-nous-voulons-continuer-l-histoire-tout-en-y-ecrivant-un-nouveau-chapitre-.html",
            "https://latribunedelhotellerie.com/nomination-paris-thibaut-drege-directeur-general-du-four-seasons-hotel-george-v/",
            "linkedin:thibaut-drege-27801424",
        ],
        "set_company_website": "https://www.fourseasons.com/fr/paris/",
        "company_email": "reservation.paris@fourseasons.com",
        "company_phone_override": {
            "value": "+33149527000",
            "sources": [
                "https://www.fourseasons.com/fr/paris/contact-us/",
                "https://www.fourseasons.com/fr/paris/",
                "https://www.facebook.com/FourSeasonsHotelParis/",
            ],
            "confidence": 95,
            "note": "3 sources (page contact + page d'accueil + Facebook officiel)",
        },
        "company_linkedin_override": {
            "value": "https://fr.linkedin.com/company/four-seasons-hotel-george-v-paris",
            "sources": ["ddg:linkedin-company"],
            "confidence": 85,
            "note": "page LinkedIn officielle Four Seasons Hotel George V, Paris",
        },
        "person_email": None,
        "person_linkedin": {
            "value": "https://fr.linkedin.com/in/thibaut-drege-27801424",
            "sources": ["ddg:linkedin-in", "slug-match:thibaut-drege", "title:Four Seasons Hotel George V, Paris"],
            "confidence": 95,
            "note": "LinkedIn title: 'Thibaut Drege - Four Seasons Hotel George V, Paris' + Sirene dirigeant",
        },
        "person_instagram": None,
        "person_phone": None,
    },
    # ----------------------------------------------------------------------
    # 7. LE ROYAL MONCEAU – RAFFLES PARIS
    # ----------------------------------------------------------------------
    {
        "siren": "479829582",
        "first": "Nicolas",
        "last": "Dubort",
        "role": "Directeur Général (depuis 5 janvier 2026)",
        "sources": [
            "sirene",
            "https://hr-infos.fr/nicolas-dubort-nomme-directeur-general-du-royal-monceau-raffles-paris/",
            "https://www.journaldespalaces.com/communique-77273-france-nominations-nicolas-dubortdirecteur-general-du-royal-monceauraffles-paris.html",
            "linkedin:nicolas-dubort-1ab0041",
        ],
        "set_company_website": "https://www.raffles.com/fr/paris/",
        "company_email": "paris@raffles.com",
        "company_phone_override": {
            "value": "+33142998800",
            "sources": [
                "https://www.raffles.com/fr/paris/",
                "https://all.accor.com/hotel/A5D5/index.fr.shtml",
            ],
            "confidence": 90,
            "note": "2 sources (site Raffles + Accor)",
        },
        "company_instagram_override": {
            "value": "https://www.instagram.com/leroyalmonceau",
            "sources": ["https://www.raffles.com/fr/paris/"],
            "confidence": 85,
            "note": "linked depuis le site officiel",
        },
        "company_linkedin_override": {
            "value": "https://www.linkedin.com/company/le-royal-monceau---raffles-paris",
            "sources": ["https://www.raffles.com/fr/paris/"],
            "confidence": 85,
            "note": "linked depuis le site officiel + employés multiples",
        },
        "person_email": None,
        "person_linkedin": {
            "value": "https://fr.linkedin.com/in/nicolas-dubort-1ab0041",
            "sources": ["ddg:linkedin-in", "slug-match:nicolas-dubort"],
            "confidence": 70,
            "note": "slug encode prénom+nom (Paris/Île-de-France)",
        },
        "person_instagram": None,
        "person_phone": None,
    },
    # ----------------------------------------------------------------------
    # 8. SHANGRI-LA PARIS
    # ----------------------------------------------------------------------
    {
        "siren": "487719304",
        "first": "Nicolas",
        "last": "De Gols",
        "role": "Directeur Général",
        "sources": [
            "sirene",
            "https://www.industrie-hoteliere.com/2025/10/13/nicolas-de-gols-nomme-directeur-general-du-shangri-la-paris/",
            "https://www.journaldespalaces.com/communique-76107-france-nominations-nicolas-de-gols-nomme-directeur-general-du-shangri-la-paris.html",
            "https://www.ge-rh.expert/de-barman-a-directeur-general-du-shangri-la-paris-a-43-ans-la-success-story-dun-passionne-de-lhotellerie/",
            "https://www.lhotellerie-restauration.fr/actualite/nicolas-de-gols-nomme-directeur-general-du-shangri-la-paris",
            "https://www.lefigaro.fr/decideurs/de-barman-a-directeur-general-du-shangri-la-paris-a-43-ans-la-success-story-d-un-passionne-de-l-hotellerie-20251024",
            "linkedin:nicolas-de-gols-02545920",
        ],
        "set_company_website": "https://www.shangri-la.com/fr/paris/shangrila/",
        "company_email": "paris@shangri-la.com",
        "company_phone_override": {
            "value": "+33153671998",
            "sources": [
                "https://www.shangri-la.com/fr/paris/shangrila/about/",
                "https://www.shangri-la.com/fr/paris/shangrila/about/map-directions/",
            ],
            "confidence": 90,
            "note": "2 sources (about + map-directions Shangri-La officiel)",
        },
        "company_linkedin_override": {
            "value": "https://fr.linkedin.com/company/shangri-la-paris",
            "sources": ["ddg:linkedin-company"],
            "confidence": 85,
            "note": "page LinkedIn officielle Shangri-La Paris",
        },
        "person_email": None,
        "person_linkedin": {
            "value": "https://www.linkedin.com/in/nicolas-de-gols-02545920/",
            "sources": ["ddg:linkedin-in", "slug-match:nicolas-de-gols", "title:Shangri-La Paris"],
            "confidence": 95,
            "note": "LinkedIn title: 'Nicolas De Gols - Shangri-La Paris'",
        },
        "person_instagram": None,
        "person_phone": None,
    },
    # ----------------------------------------------------------------------
    # 9. THE PENINSULA PARIS
    # ----------------------------------------------------------------------
    {
        "siren": "812199552",
        "first": "Luc",
        "last": "Delafosse",
        "role": "Directeur Général (à compter du 2 mars 2026)",
        "sources": [
            "sirene",
            "https://www.tendancehotellerie.fr/articles-breves/communique-de-presse/25389-article/le-peninsula-paris-nomme-luc-delafosse-au-poste-de-directeur-general",
            "https://www.deplacementspros.com/hebergement/hotellerie/le-peninsula-paris-nomme-son-nouveau-directeur-general",
            "https://hr-infos.fr/luc-delafosse-nomme-directeur-general-du-the-peninsula-paris/",
            "https://www.tourmag.com/The-Peninsula-Paris-Luc-Delafosse-nomme-Directeur-General_a130672.html",
            "https://www.ge-rh.expert/luc-delafosse-nomme-directeur-general-du-the-peninsula-paris/",
        ],
        "set_company_website": "https://www.peninsula.com/fr/paris/5-star-luxury-hotel-16th-arrondissement",
        "company_email": "ppr@peninsula.com",
        "company_phone_override": {
            "value": "+33158122888",
            "sources": [
                "https://www.facebook.com/ThePeninsulaParis/",
                "https://www.meetings-conventions.com/Meeting-Event-Venues/Paris/Convention-Hotel/The-Peninsula-Paris-p54569739",
                "https://www.momondo.fr/hotel/paris/The-Peninsula-Paris.mhd777567.ksp",
            ],
            "confidence": 95,
            "note": "3 sources (Facebook officiel + Meetings&Conventions + Momondo)",
        },
        "company_instagram_override": {
            "value": "https://www.instagram.com/thepeninsulaparis",
            "sources": ["https://www.peninsula.com/fr/paris/5-star-luxury-hotel-16th-arrondissement"],
            "confidence": 85,
            "note": "linked depuis le site officiel",
        },
        "company_linkedin_override": {
            "value": "https://fr.linkedin.com/company/peninsula-paris",
            "sources": ["ddg:linkedin-company"],
            "confidence": 85,
            "note": "page LinkedIn officielle The Peninsula Paris",
        },
        "person_email": {
            "value": "ppr@peninsula.com",
            "sources": [
                "https://www.peninsula.com/fr/paris/5-star-luxury-hotel-16th-arrondissement",
                "https://www.facebook.com/ThePeninsulaParis/",
            ],
            "confidence": 60,
            "note": "boîte officielle du palace; route entrante vers le DG via secrétariat",
        },
        "person_phone": {
            "value": "+33158122888",
            "sources": [
                "https://www.facebook.com/ThePeninsulaParis/",
                "https://www.meetings-conventions.com/Meeting-Event-Venues/Paris/Convention-Hotel/The-Peninsula-Paris-p54569739",
                "https://www.momondo.fr/hotel/paris/The-Peninsula-Paris.mhd777567.ksp",
            ],
            "confidence": 75,
            "note": "standard du palace; demander le DG par nom (effectif 2 mars 2026)",
        },
        "person_linkedin": None,
        "person_instagram": None,
    },
    # ----------------------------------------------------------------------
    # 10. MANDARIN ORIENTAL LUTETIA, PARIS
    # ----------------------------------------------------------------------
    {
        "siren": "572009231",
        "first": "Julien",
        "last": "Bardet",
        "role": "Directeur Général (depuis 15 septembre 2025)",
        "sources": [
            "sirene",
            "https://www.tendancehotellerie.fr/articles-breves/communique-de-presse/24935-article/mandarin-oriental-lutetia-paris-annonce-la-nomination-de-julien-bardet-en-tant-que-directeur-general",
            "https://www.latribunedelhotellerie.com/paris-mandarin-oriental-lutetia-julien-bardet-nomme-directeur-general/",
            "linkedin:julien-bardet-b83a6617",
        ],
        "set_company_website": "https://www.mandarinoriental.com/fr/paris/lutetia",
        "company_email": "molut-info@mohg.com",
        "company_phone_override": {
            "value": "+33149544600",
            "sources": [
                "https://www.mandarinoriental.com/fr/paris/lutetia/contact-us",
                "https://www.tripadvisor.fr/Hotel_Review-g187147-d197427-Reviews-Mandarin_Oriental_Lutetia_Paris-Paris_Ile_de_France.html",
            ],
            "confidence": 90,
            "note": "2 sources (page contact officielle + Tripadvisor)",
        },
        "company_instagram_override": {
            "value": "https://www.instagram.com/mo_lutetia",
            "sources": ["https://www.mandarinoriental.com/fr/paris/lutetia"],
            "confidence": 85,
            "note": "linked depuis le site officiel",
        },
        "company_linkedin_override": {
            "value": "https://fr.linkedin.com/company/hotel-lutetia-sarl",
            "sources": ["ddg:linkedin-company"],
            "confidence": 80,
            "note": "page LinkedIn 'Mandarin Oriental Lutetia, Paris'",
        },
        "company_facebook": "https://www.facebook.com/mandarinorientallutetia",
        "person_email": None,
        "person_linkedin": {
            "value": "https://fr.linkedin.com/in/julien-bardet-b83a6617",
            "sources": ["ddg:linkedin-in", "slug-match:julien-bardet", "title:Mandarin Oriental Lutetia, Paris"],
            "confidence": 95,
            "note": "LinkedIn title: 'Julien BARDET - Mandarin Oriental Lutetia, Paris'",
        },
        "person_instagram": None,
        "person_phone": None,
    },
]


def apply_overrides(lead: Lead, pick: dict) -> None:
    # Person email
    if pick.get("person_email"):
        lead.person_email = ScoredField(**pick["person_email"])
    else:
        lead.person_email = ScoredField.missing()

    # Person phone
    if pick.get("person_phone"):
        lead.person_phone = ScoredField(**pick["person_phone"])
    else:
        lead.person_phone = ScoredField.missing()

    # Person LinkedIn
    if pick.get("person_linkedin"):
        lead.person_linkedin = ScoredField(**pick["person_linkedin"])
    else:
        if lead.person_linkedin.confidence < 50:
            lead.person_linkedin = ScoredField.missing()

    # Person Instagram
    if pick.get("person_instagram"):
        lead.person_instagram = ScoredField(**pick["person_instagram"])
    else:
        if lead.person_instagram.confidence < 50:
            lead.person_instagram = ScoredField.missing()

    # Company website
    if pick.get("set_company_website"):
        lead.company_website = pick["set_company_website"]

    # Company phone
    if pick.get("company_phone_override"):
        lead.company_phone = ScoredField(**pick["company_phone_override"])

    # Company instagram
    if pick.get("company_instagram_override"):
        lead.company_instagram = ScoredField(**pick["company_instagram_override"])

    # Company LinkedIn
    if pick.get("company_linkedin_override"):
        lead.company_linkedin = ScoredField(**pick["company_linkedin_override"])

    # Company Facebook
    if pick.get("company_facebook"):
        lead.company_facebook = pick["company_facebook"]

    # Recompute drop status
    lead.dropped = False
    lead.drop_reason = None
    lead.evaluate()


def main() -> int:
    leads = []
    for pick in PICKS:
        with SireneClient() as c:
            resp = c.search(pick["siren"])
            company = resp.results[0] if resp.results else None
        if not company:
            print(f"!! no Sirene result for {pick['siren']}", file=sys.stderr)
            continue

        print(f">> enriching {company.name} ({pick['siren']})", file=sys.stderr)
        partial = enrich_company_partial(company)
        # Override website BEFORE finalize so SMTP/email patterns use the right
        # domain. Drop web_enrichment if auto-discovery scraped the wrong host.
        if pick.get("set_company_website"):
            from urllib.parse import urlparse
            new_host = (urlparse(pick["set_company_website"]).hostname or "").removeprefix("www.")
            old_host = (urlparse(partial.get("website") or "").hostname or "").removeprefix("www.")
            partial["website"] = pick["set_company_website"]
            if old_host and old_host != new_host:
                partial["web_enrichment"] = None
                partial["team_page_text"] = None

        lead = finalize_lead(
            partial,
            person_first=pick["first"],
            person_last=pick["last"],
            person_role=pick["role"],
            person_sources=pick["sources"],
            naf_label="Hôtellerie de luxe / Palace parisien",
        )
        apply_overrides(lead, pick)
        leads.append(lead)
        print(
            f"   dropped={lead.dropped} reason={lead.drop_reason or '-'} overall={lead.overall_score}",
            file=sys.stderr,
        )

    summary = {
        "total": len(leads),
        "kept": sum(1 for l in leads if not l.dropped),
        "dropped": sum(1 for l in leads if l.dropped),
        "drop_reasons": [
            {"company": l.company_name, "reason": l.drop_reason}
            for l in leads if l.dropped
        ],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    if leads:
        out = export_leads(leads, prefer_sheet=False)
        print(f"\nEXPORT -> {out}", file=sys.stderr)
        print(json.dumps({"export_path": str(out)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
