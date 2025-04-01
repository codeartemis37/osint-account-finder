import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
import requests
from difflib import SequenceMatcher
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

class UsernameChecker:
    def __init__(self, username):
        self.username = username
        self.results = defaultdict(lambda: {'exact': [], 'search': []})
        self.lock = threading.Lock()
        self.sites = self.load_sites('sites.json')
        self.seen_exact = set()
        self.seen_search = set()
        self.linked_accounts_scores = defaultdict(lambda: defaultdict(int))
        self.headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}

    def load_sites(self, filename):
        json_file_path = os.path.join(os.path.dirname(__file__), filename)
        with open(json_file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        sites = []
        for category, site_list in data.items():
            for site in site_list:
                sites.append((category, site['name'], site['url_pattern'], site.get('search_url', '')))
        return sites

    def similarity_score(self, a, b):
        return SequenceMatcher(None, a.lower(), b.lower()).ratio()

    def is_valid_url(self, url):
        try:
            result = urlparse(url)
            return all([result.scheme, result.netloc])
        except ValueError:
            return False

    def extract_search_results(self, html_content, search_url):
        soup = BeautifulSoup(html_content, 'html.parser')
        
        error_messages = ["Aucun", "No results found", "nobody", "Sorry", "banned", "incorrect", "cannot be found", "404", "nothing"]
        for message in error_messages:
            if message.lower() in soup.get_text().lower():
                return []

        potential_results = soup.find_all(lambda tag: tag.name and self.username.lower() in tag.get_text().lower())
        
        results = []
        for element in potential_results:
            full_text = element.get_text(strip=True)
            link = element.find('a') or element.find_parent('a')
            url = urljoin(search_url, link['href']) if link and 'href' in link.attrs else None
            
            score = self.similarity_score(self.username, full_text)
            
            if score > 0.5 and url:
                results.append({'url': url, 'text': full_text, 'score': score})
        
        return sorted(results, key=lambda x: x['score'], reverse=True)

    def check_site(self, category, name, url_pattern, search_url):
        exact_url = url_pattern.replace('$pseudo', self.username)
        
        if not self.is_valid_url(exact_url):
            print(f"URL non valide : {exact_url}")
            return

        try:
            with requests.Session() as session:
                response = session.get(exact_url, headers=self.headers, timeout=30)
                if response.status_code == 200:
                    with self.lock:
                        if exact_url not in self.seen_exact:
                            self.results[category]['exact'].append((name, exact_url))
                            self.seen_exact.add(exact_url)

                if search_url:
                    search_response = session.get(search_url.replace('$pseudo', self.username), headers=self.headers, timeout=30)
                    if search_response.status_code == 200:
                        search_results = self.extract_search_results(search_response.text, search_url)
                        with self.lock:
                            for result in search_results:
                                if result['url'] not in self.seen_search:
                                    self.results[category]['search'].append((name, result))
                                    self.seen_search.add(result['url'])

        except requests.exceptions.RequestException as e:
            print(f"Erreur lors de la vérification de {category} - {name}: {str(e)}")

    def find_linked_accounts(self, user):
        linked_accounts = defaultdict(set)
    
        for category, data in self.results.items():
            for name, base_url in data['exact']:
                try:
                    response = requests.get(base_url, headers=self.headers, timeout=30)
                    response.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)
                    soup = BeautifulSoup(response.text, 'html.parser')
                    links = soup.find_all('a', href=True)
    
                    parsed_base_url = urlparse(base_url.lower())
    
                    for link in links:
                        href = link['href']
                        absolute_url = urljoin(base_url, href)  # Ensure it's an absolute URL
                        parsed_href = urlparse(absolute_url.lower())
    
                        # Check if the link is external
                        if parsed_href.netloc and parsed_href.netloc != parsed_base_url.netloc:
                            # Check if the username is in the URL *as a username component*
                            # This is a heuristic and might need adjustment
                            path_segments = parsed_href.path.split('/')
                            if user in path_segments:
                                # Find the site name corresponding to the linked URL
                                linked_site_name = None
                                for _, site_name, site_url, _ in self.sites:
                                    parsed_site_url = urlparse(site_url.lower().replace('$pseudo', user))
                                    if parsed_href.netloc == parsed_site_url.netloc:
                                        linked_site_name = site_name
                                        break  # Stop after the first match
    
                                if linked_site_name:
                                    linked_accounts[linked_site_name].add(absolute_url)
    
                except requests.exceptions.RequestException as e:
                        print(f"Erreur lors de la recherche de comptes liés pour {name}: {str(e)}")
                except Exception as e:
                    print(f"Erreur inattendue lors de la recherche de comptes liés pour {name}: {type(e).__name__} - {str(e)}")
    
    
        # Convert sets to lists for consistent output format
        linked_accounts_list = {site: list(urls) for site, urls in linked_accounts.items()}
        return linked_accounts_list
        
    def find_real_identity(self):
        real_identity = {}
        for category, data in self.results.items():
            for name, url in data['exact']:
                try:
                    response = requests.get(url, headers=self.headers, timeout=30)
                    if response and response.raw._connection and response.raw._connection.sock:
                        ip = response.raw._connection.sock.getpeername()[0]
                        real_identity[name] = {'ip': ip}
                    else:
                        real_identity[name] = {'ip': 'Non disponible'}
                    
                    soup = BeautifulSoup(response.text, 'html.parser')
                    potential_info = soup.find_all(['h1', 'h2', 'h3', 'p'], class_=['name', 'location', 'bio'])
                    for info in potential_info:
                        if 'name' in info.get('class', []):
                            real_identity[name]['real_name'] = info.text.strip()
                        elif 'location' in info.get('class', []):
                            real_identity[name]['location'] = info.text.strip()
                        elif 'bio' in info.get('class', []):
                            real_identity[name]['bio'] = info.text.strip()
                except requests.exceptions.RequestException as e:
                    print(f"Erreur lors de la recherche de l'identité réelle pour {name}: {str(e)}")
        return real_identity

    def run(self):
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(self.check_site, cat, name, url, search_url) for cat, name, url, search_url in self.sites]
            
            for future in as_completed(futures):
                future.result()

        linked_accounts = self.find_linked_accounts(pseudo)
        real_identity = self.find_real_identity()

        return self.results, linked_accounts, real_identity

if __name__ == "__main__":
    pseudo = input("Entrez le pseudo à rechercher: ")
    checker = UsernameChecker(pseudo)
    results, linked_accounts, real_identity = checker.run()

    for category, data in results.items():
        print(f"\n=== {category.upper()} ===")
        if data['exact']:
            for name, url in data['exact']:
                print(f"[100.00%] {name}: {url}")
        if data['search']:
            for name, result in data['search']:
                print(f"[{result['score']*100:6.2f}%] {name}: {result['url']}")

    print("\n=== COMPTES LIÉS ===")
    for site, urls in linked_accounts.items():
        print(f"|-+ [{site}]:")
        for url in urls:
            print(f"| |- {url}")

    print("\n=== IDENTITÉ RÉELLE / IP ===")
    for account, identity in real_identity.items():
        print(f"| + {account}:")
        for key, value in identity.items():
            print(f"| |- {key}: {value}")
    input()
