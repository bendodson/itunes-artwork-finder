var pathToAPI = 'https://example/com/api.php'; // replace with your path to the api.php file

var countries = {
    ae: 'United Arab Emirates',
    ag: 'Antigua and Barbuda',
    ai: 'Anguilla',
    al: 'Albania',
    am: 'Armenia',
    ao: 'Angola',
    ar: 'Argentina',
    at: 'Austria',
    au: 'Australia',
    az: 'Azerbaijan',
    bb: 'Barbados',
    be: 'Belgium',
    bf: 'Burkina-Faso',
    bg: 'Bulgaria',
    bh: 'Bahrain',
    bj: 'Benin',
    bm: 'Bermuda',
    bn: 'Brunei Darussalam',
    bo: 'Bolivia',
    br: 'Brazil',
    bs: 'Bahamas',
    bt: 'Bhutan',
    bw: 'Botswana',
    by: 'Belarus',
    bz: 'Belize',
    ca: 'Canada',
    cg: 'Democratic Republic of the Congo',
    ch: 'Switzerland',
    cl: 'Chile',
    cn: 'China',
    co: 'Colombia',
    cr: 'Costa Rica',
    cv: 'Cape Verde',
    cy: 'Cyprus',
    cz: 'Czech Republic',
    de: 'Germany',
    dk: 'Denmark',
    dm: 'Dominica',
    do: 'Dominican Republic',
    dz: 'Algeria',
    ec: 'Ecuador',
    ee: 'Estonia',
    eg: 'Egypt',
    es: 'Spain',
    fi: 'Finland',
    fj: 'Fiji',
    fm: 'Federated States of Micronesia',
    fr: 'France',
    gb: 'United Kingdom',
    gd: 'Grenada',
    gh: 'Ghana',
    gm: 'Gambia',
    gr: 'Greece',
    gt: 'Guatemala',
    gw: 'Guinea Bissau',
    gy: 'Guyana',
    hk: 'Hong Kong',
    hn: 'Honduras',
    hr: 'Croatia',
    hu: 'Hungary',
    id: 'Indonesia',
    ie: 'Ireland',
    il: 'Israel',
    in: 'India',
    is: 'Iceland',
    it: 'Italy',
    jm: 'Jamaica',
    jo: 'Jordan',
    jp: 'Japan',
    ke: 'Kenya',
    kg: 'Krygyzstan',
    kh: 'Cambodia',
    kn: 'Saint Kitts and Nevis',
    kr: 'South Korea',
    kw: 'Kuwait',
    ky: 'Cayman Islands',
    kz: 'Kazakhstan',
    la: 'Laos',
    lb: 'Lebanon',
    lc: 'Saint Lucia',
    lk: 'Sri Lanka',
    lr: 'Liberia',
    lt: 'Lithuania',
    lu: 'Luxembourg',
    lv: 'Latvia',
    md: 'Moldova',
    mg: 'Madagascar',
    mk: 'Macedonia',
    ml: 'Mali',
    mn: 'Mongolia',
    mo: 'Macau',
    mr: 'Mauritania',
    ms: 'Montserrat',
    mt: 'Malta',
    mu: 'Mauritius',
    mw: 'Malawi',
    mx: 'Mexico',
    my: 'Malaysia',
    mz: 'Mozambique',
    na: 'Namibia',
    ne: 'Niger',
    ng: 'Nigeria',
    ni: 'Nicaragua',
    nl: 'Netherlands',
    np: 'Nepal',
    no: 'Norway',
    nz: 'New Zealand',
    om: 'Oman',
    pa: 'Panama',
    pe: 'Peru',
    pg: 'Papua New Guinea',
    ph: 'Philippines',
    pk: 'Pakistan',
    pl: 'Poland',
    pt: 'Portugal',
    pw: 'Palau',
    py: 'Paraguay',
    qa: 'Qatar',
    ro: 'Romania',
    ru: 'Russia',
    sa: 'Saudi Arabia',
    sb: 'Soloman Islands',
    sc: 'Seychelles',
    se: 'Sweden',
    sg: 'Singapore',
    si: 'Slovenia',
    sk: 'Slovakia',
    sl: 'Sierra Leone',
    sn: 'Senegal',
    sr: 'Suriname',
    st: 'Sao Tome e Principe',
    sv: 'El Salvador',
    sz: 'Swaziland',
    tc: 'Turks and Caicos Islands',
    td: 'Chad',
    th: 'Thailand',
    tj: 'Tajikistan',
    tm: 'Turkmenistan',
    tn: 'Tunisia',
    tr: 'Turkey',
    tt: 'Republic of Trinidad and Tobago',
    tw: 'Taiwan',
    tz: 'Tanzania',
    ua: 'Ukraine',
    ug: 'Uganda',
    us: 'United States of America',
    uy: 'Uruguay',
    uz: 'Uzbekistan',
    vc: 'Saint Vincent and the Grenadines',
    ve: 'Venezuela',
    vg: 'British Virgin Islands',
    vn: 'Vietnam',
    ye: 'Yemen',
    za: 'South Africa',
    zw: 'Zimbabwe'
}

function getSearchParameters() {
      var prmstr = window.location.search.substr(1);
      return prmstr != null && prmstr != "" ? transformToAssocArray(prmstr) : {};
}

function transformToAssocArray( prmstr ) {
    var params = {};
    var prmarr = prmstr.split("&");
    for ( var i = 0; i < prmarr.length; i++) {
        var tmparr = prmarr[i].split("=");
        params[tmparr[0]] = decodeURIComponent(tmparr[1]);
    }
    return params;
}

function performSearch() {
    $('#results').html('');
    $('#results').append('<h3>Searching...</h3>');

    var query = $('#query').val();
    if (!query.length) {
        return false;
    };

    var entity = ($('#entity').val()) ? $('#entity').val() : 'tvSeason';
    var country = ($('#country').val()) ? $('#country').val() : 'us';
    
    $.ajax({
        type: "GET",
        crossDomain: true,
        url: 'https://itunesartwork.bendodson.com/api.php',
        data: {query: query, entity: entity, country: country, type: 'request'},
        dataType: 'json'
    }).done(function(data) {

        $.ajax({

            type: "GET",
            crossDomain: true,
            url: data.url,
            data: {},
            dataType: 'jsonp'

        }).done(function(data) {

            console.log(data);

            $.ajax({

                type: "POST",
                crossDomain: true,
                url: pathToAPI,
                data: {json: JSON.stringify(data), type: 'data', entity: entity},
                dataType: 'json'

            }).done(function(data) {

                $('#results').html('');
                if (data.error) {
                        $('#results').append('<h3>'+data.error+'</h3>');
                } else {
                    if (!data.length) {
                        $('#results').append('<h3>No results found.</h3>');
                    } else {
                        for (var i = 0; i < data.length; i++) {
                            var result = data[i];
                            console.log(result.title);

                            var html = '<div><h3>'+result.title+'</h3>';
                            if (entity != 'software' && entity != 'iPadSoftware' && entity != 'macSoftware') {
                                html += '<p><a href="'+result.url+'" target="_blank">Standard Resolution</a> | <a href="'+result.hires+'" target="_blank">High Resolution</a> <em><small>'+result.warning+'</small></em></p>';
                            } else if (entity == 'software' || entity == 'iPadSoftware') {
                                html += '<p><a href="./app/?url='+encodeURIComponent(result.appstore)+'&country='+country+'" target="_blank">View screenshots / videos</a></p>';
                            }
                            html += '<a href="'+result.url+'" target="_blank"><img src="'+result.url+'" alt="iTunes Artwork for \''+result.title+'\'" width="'+result.width+'" height="'+result.height+'"></a>';
                            html += '</div>';

                            $('#results').append(html);
                        };
                    }            
                }
                $('#results').append('<p>If the item you are searching for is not available on iTunes, this tool will not find it. Please do not email me asking for specific items if they are not available on iTunes! I recommend both <a href="https://bitbucket.org/galad87/subler/">Subler</a> and <a href="https://www.google.co.uk/imghp?gws_rd=ssl">Google Image Search</a> as good alternative places to find artwork.</p>');

            });
        });
    });
}

$(document).ready(function() {	


	var sortable = [];
	for (var key in countries) {
		sortable.push([key, countries[key]]);	
	}
    sortable.sort(function(a, b) {
    	if(a[1] < b[1]) return 1;
	    if(a[1] > b[1]) return -1;
	    return 0;
    });
	
	for (var i = sortable.length - 1; i >= 0; i--) {
		var array = sortable[i];
		$('#country').append('<option value="'+array[0]+'">'+array[1]+'</option>');
	};

    var params = getSearchParameters();
    if (params.entity && params.query && params.country) {
        $('#query').val(params.query);
        $('#entity').val(params.entity);
        $('#country').val(params.country);
        performSearch();
    };

	$('#iTunesSearch').submit(function() {
		performSearch();
		return false;
	});


});