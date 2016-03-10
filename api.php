<?php
header("Access-Control-Allow-Origin: *");


$search = $_GET['query'];
$results = array();
if ($search) {
	$entity = ($_GET['entity']) ? $_GET['entity'] : 'tvSeason';
	$country = ($_GET['country']) ? $_GET['country'] : 'us';
	$width = 600;
	$height = 600;
	$warning = '';

	$shortFilm = false;
	if ($entity == 'shortFilm') {
		$shortFilm = true;
		$entity = 'movie';
	}
	$url = 'http://ax.itunes.apple.com/WebObjects/MZStoreServices.woa/wa/wsSearch?term='.urlencode($search).'&country='.$country.'&entity='.$entity;
	if ($shortFilm) {
		$url .= '&attribute=shortFilmTerm';
		$entity = 'shortFilm';
	}
	if ($_GET['entity'] == 'id' || $_GET['entity'] == 'idAlbum') {
		$url = 'https://itunes.apple.com/lookup?id='.urlencode($search).'&country='.$_GET['country'];
	}
	
	$ch = curl_init(); 
	curl_setopt($ch, CURLOPT_URL, $url); 
	curl_setopt($ch, CURLOPT_CONNECTTIMEOUT, 5); 
	curl_setopt($ch, CURLOPT_FOLLOWLOCATION, true);
	curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
	$json = curl_exec($ch); 

	if ($errno = curl_errno($ch)) {
	    echo json_encode(array('error' => "We were unable to connect to iTunes. Please try again shortly."));
	    exit;
	}

	curl_close($ch);    

	$obj = json_decode($json);


	foreach ($obj->results as $result) {
		
		$data = array();
		$data['url'] = str_replace('100x100', '600x600', $result->artworkUrl100);

		$hires = str_replace('100x100bb', '100000x100000-999', $result->artworkUrl100);
		$parts = parse_url($hires);
		$hires = 'http://is5.mzstatic.com'.$parts['path'];

		$data['hires'] = $hires;
		$data['title'] = ($entity == 'movie') ? $result->trackName : $result->collectionName;

		switch ($entity) {
			case 'musicVideo':
				$data['title'] = $result->trackName.' (by '.$result->artistName.')';
				$data['url'] = $hires;
				$width = 640;
				$height = 464;
				break;
			case 'tvSeason':
				$data['title'] = $result->collectionName;
				break;
			case 'movie':
			case 'id':
			case 'shortFilm':
				$data['title'] = $result->trackName;
				$width = 400;
				$warning = '(may not work)';
				break;
			case 'ebook':
				$data['title'] = $result->trackName.' (by '.$result->artistName.')';
				$width = 400;
				$warning = '(probably won\'t work)';
				break;
			case 'album':
			case 'idAlbum':
				$data['title'] = $result->collectionName.' (by '.$result->artistName.')';
				//$warning = '(probably won\'t work)';
				break;
			case 'audiobook':
				$data['title'] = $result->collectionName.' (by '.$result->artistName.')';
				$warning = '(probably won\'t work)';
				break;
			case 'podcast':
				$data['title'] = $result->collectionName.' (by '.$result->artistName.')';
				break;
			case 'software':
				$data['url'] = str_replace('512x512bb', '1024x1024bb', $result->artworkUrl512);
				$data['appstore'] = $result->trackViewUrl;
				$data['title'] = $result->trackName;
				$width = 512;
				$height = 512;
				break;
			default:
				break;
		}

		if ($data['title']) {
			$data['width'] = $width;
			$data['height'] = $height;
			$data['warning'] = $warning;
			$results[] = $data;	
		}
	}
}

echo json_encode($results);

?>
