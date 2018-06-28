<?php
header("Access-Control-Allow-Origin: *");

if (isset($_GET['type']) && $_GET['type'] == 'request') {
	$search = $_GET['query'];
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
		$url = 'https://itunes.apple.com/search?term='.urlencode($search).'&country='.$country.'&entity='.$entity;
		if ($shortFilm) {
			$url .= '&attribute=shortFilmTerm';
			$entity = 'shortFilm';
		}
		if ($_GET['entity'] == 'id' || $_GET['entity'] == 'idAlbum') {
			$url = 'https://itunes.apple.com/lookup?id='.urlencode($search).'&country='.$_GET['country'];
		}
		$url .= '&limit=25';
		echo json_encode(["url" => $url]);
		exit;
	}
}

if (isset($_POST['type']) && $_POST['type'] == 'data') {

	$output = array();

	$json = json_decode($_POST['json']);
	$entity = ($_POST['entity']) ? $_POST['entity'] : 'tvSeason';

	foreach ($json->results as $result) {

		if (($_POST['entity'] == 'id' && $result->kind != 'feature-movie') && ($_POST['entity'] == 'id' && $result->wrapperType != 'collection')) {
			continue;
		}

		if ($_POST['entity'] == 'idAlbum' && $result->collectionType != 'Album') {
			continue;
		}

		$width = 600;
		$height = 600;
		
		$data = array();
		$data['url'] = str_replace('100x100', '600x600', $result->artworkUrl100);

		$hires = str_replace('100x100bb', '100000x100000-999', $result->artworkUrl100);
		$parts = parse_url($hires);
		$hires = 'http://is5.mzstatic.com'.$parts['path'];

		$data['hires'] = $hires;
		$data['title'] = ($entity == 'movie') ? $result->trackName : $result->collectionName;

		$warning = '';

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
				if (!$data['title']) {
					$data['title'] = $result->collectionName;
				}
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
			case 'iPadSoftware':
			case 'macSoftware':
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
			$output[] = $data;	
		}
	}

	echo json_encode($output);
	exit;
}


?>
